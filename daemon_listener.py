#!/usr/bin/env python3
"""
Lab Data Mount Daemon Listener (Unprivileged Flask Web Gateway)
==============================================================
Runs as an unprivileged user (e.g., www-data).
Receives HTTP requests, validates headers (HMAC/mTLS), and forwards commands
to the privileged Daemon Controller via a local Unix socket.
"""

import os
import sys
import json
import logging
import socket
from pathlib import Path

import yaml
from flask import Flask, jsonify, request

# Import custom modules
from modules.state_db import StateDB
from modules.socket_comm import send_msg, recv_msg

# ---------------------------------------------------------------------------
# Logging Setup
# ---------------------------------------------------------------------------
log = logging.getLogger("lab-daemon")
log.setLevel(logging.INFO)
if not log.handlers:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter('%(asctime)s [%(levelname)s] (Listener) %(message)s'))
    log.addHandler(handler)

# ---------------------------------------------------------------------------
# Configuration Loading
# ---------------------------------------------------------------------------
DEFAULT_CONFIG = {
    "storage": {
        "base_path": "/srv/labdata",
        "users_path": "/srv/labdata/users",
        "groups_path": "/srv/labdata/groups",
        "sessions_path": "/tmp/labdata/sessions",
        "public_path": "/srv/labdata/public",
        "group_folder_type": "project"
    },
    "session": {
        "db_path": "/var/lib/lab-daemon/state.db",
        "socket_path": "/var/run/lab-daemon/lab-daemon.sock"
    },
    "mtls": {
        "enabled": False,
        "ca_cert": "certs/ca.crt",
        "server_cert": "certs/server.crt",
        "server_key": "certs/server.key"
    }
}

config = DEFAULT_CONFIG.copy()

config_path = os.environ.get("LAB_DAEMON_CONFIG")
if not config_path:
    for p in ("/etc/lab-daemon/config.yaml", "config.yaml"):
        if os.path.exists(p):
            config_path = p
            break

if config_path:
    try:
        with open(config_path, "r") as f:
            user_config = yaml.safe_load(f)
            if user_config:
                for key in ("storage", "session", "mtls"):
                    if key in user_config and isinstance(user_config[key], dict):
                        config[key].update(user_config[key])
        log.info(f"Loaded configuration from: {config_path}")
    except Exception as e:
        log.error(f"Error loading configuration from {config_path}: {e}")

# Initialize Database for Read-only GET queries
state_db = StateDB(config["session"]["db_path"])

# Initialize Flask
app = Flask(__name__)

# ---------------------------------------------------------------------------
# Request Verification & Validation Helpers
# ---------------------------------------------------------------------------
def verify_client_auth() -> bool:
    """
    Verify Mutual TLS (mTLS) client certificate verification from Nginx proxy.
    """
    client_verify = request.headers.get("X-Client-Verify")
    if client_verify:
        if client_verify != "SUCCESS":
            log.warning(f"🔐 mTLS verification failed: X-Client-Verify is '{client_verify}'")
            return False
        return True
    return True

def _validate_request():
    """
    Parse and validate incoming request body parameters.
    """
    data = request.get_json(silent=True)
    if data is None:
        return None, ({"error": "Invalid JSON"}, 400)

    user_id = data.get("user_id")
    tool = data.get("tool", "").strip()
    account_id = data.get("account_id")
    project_id = data.get("project_id")

    if user_id is None or not tool:
        return None, ({"error": "'user_id' and 'tool' required"}, 400)

    if account_id is None or project_id is None:
        return None, ({"error": "'account_id' and 'project_id' required"}, 400)

    try:
        user_id_str = str(user_id).strip()
        account_id_int = int(account_id)
        project_id_int = int(project_id)
    except Exception:
        return None, ({"error": "Invalid ID format — user_id, account_id, project_id must be integers"}, 400)

    for name in (user_id_str, tool):
        if "/" in name or "\\" in name or name in (".", ".."):
            return None, ({"error": "Invalid characters in user_id or tool"}, 400)

    return {
        "user_id": user_id,
        "user_id_str": user_id_str,
        "tool": tool,
        "account_id": account_id_int,
        "project_id": project_id_int,
        "session_id": data.get("session_id")
    }, None

def _send_to_controller(action: str, payload: dict) -> tuple:
    """
    Connect to local Unix socket, send command, read and return the response.
    """
    socket_path = config["session"]["socket_path"]
    try:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.connect(socket_path)
        send_msg(sock, {"action": action, "payload": payload})
        resp = recv_msg(sock)
        sock.close()
        
        if not resp:
            return {"error": "Empty response from system backend"}, 500
        
        status = resp.get("status")
        code = resp.get("code", 200)
        res_data = resp.get("result", {})
        if status == "error":
            return res_data or {"error": resp.get("error", "Unknown error")}, code
        return res_data, code
    except Exception as e:
        log.error(f"Error communicating with Daemon Controller: {e}")
        return {"error": f"System backend daemon is unreachable: {e}"}, 503

# ---------------------------------------------------------------------------
# API Endpoints
# ---------------------------------------------------------------------------
@app.route("/mount", methods=["POST"])
def mount():
    data, err = _validate_request()
    if err:
        return jsonify(err[0]), err[1]

    if config.get("mtls", {}).get("enabled") and not verify_client_auth():
        return jsonify({"error": "Mutual TLS client verification failed"}), 401

    res, code = _send_to_controller("mount", data)
    return jsonify(res), code

@app.route("/unmount", methods=["POST"])
def unmount():
    data, err = _validate_request()
    if err:
        return jsonify(err[0]), err[1]

    if config.get("mtls", {}).get("enabled") and not verify_client_auth():
        return jsonify({"error": "Mutual TLS client verification failed"}), 401

    res, code = _send_to_controller("unmount", data)
    return jsonify(res), code

@app.route("/init_user", methods=["POST"])
def init_user():
    data = request.get_json(silent=True)
    if data is None:
        return jsonify({"error": "Invalid JSON"}), 400

    user_id = data.get("user_id")
    if user_id is None:
        return jsonify({"error": "'user_id' required"}), 400

    if config.get("mtls", {}).get("enabled") and not verify_client_auth():
        return jsonify({"error": "Mutual TLS client verification failed"}), 401

    res, code = _send_to_controller("init_user", {"user_id": user_id})
    return jsonify(res), code

@app.route("/sessions", methods=["GET"])
def get_sessions():
    """
    Query state DB directly for active sessions.
    """
    return jsonify(state_db.get_all_sessions()), 200

@app.route("/projects/<int:user_id>", methods=["GET"])
def get_projects(user_id):
    """
    Query state DB directly for user projects list.
    """
    return jsonify(state_db.get_project_linux_groups(user_id)), 200

@app.route("/quota/<username_or_id>", methods=["GET"])
def get_quota(username_or_id):
    """
    Forward quota check request to Controller.
    """
    res, code = _send_to_controller("quota", {"username_or_id": username_or_id})
    return jsonify(res), code

@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok"}), 200

if __name__ == "__main__":
    port = int(os.environ.get("LAB_DAEMON_PORT", 8080))
    app.run(host="127.0.0.1", port=port)
