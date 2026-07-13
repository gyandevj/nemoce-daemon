#!/usr/bin/env python3
"""
Lab Data Mount Daemon (Monolithic Single-Server Version)
=======================================================
Runs as root. Exposes Flask API endpoints directly to NEMO.
Handles low-level operations (mount, umount, setfacl, setquota).
Also hosts background threads for NemoSync and IdleMonitor.
Secured by HMAC-SHA256 signature verification.
"""

import os
import sys
import time
import json
import socket
try:
    import grp
    import pwd
except ImportError:
    grp = None
    pwd = None
import ctypes
import ctypes.util
import logging
import subprocess
import threading
import signal
import hashlib
import hmac
from pathlib import Path

import yaml
from flask import Flask, jsonify, request

# Import custom modules
from modules.state_db import StateDB
from modules.nemo_api_client import NemoAPIClient
from modules.user_provisioner import UserProvisioner
from modules.samba_controller import SambaController
from modules.nemo_sync import NemoSync
from idle_monitor import IdleMonitor
import acl_manager
import quota_manager

# ---------------------------------------------------------------------------
# Logging Setup
# ---------------------------------------------------------------------------
log = logging.getLogger("lab-daemon")
log.setLevel(logging.INFO)
if not log.handlers:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter('%(asctime)s [%(levelname)s] (Daemon) %(message)s'))
    log.addHandler(handler)

# ---------------------------------------------------------------------------
# Configuration Loading
# ---------------------------------------------------------------------------
DEFAULT_CONFIG = {
    "shared_secret": "00d57012a01b31f8364ebdcda42f05d15c3fd5585c69be1b8cdec1c30caa3af7",
    "storage": {
        "base_path": "/srv/labdata",
        "users_path": "/srv/labdata/users",
        "groups_path": "/srv/labdata/groups",
        "sessions_path": "/srv/labdata/sessions",
        "public_path": "/srv/labdata/public",
        "group_folder_type": "project",
        "exclude_project_ids": [],
        "exclude_account_ids": [],
        "exclude_project_names": [],
        "exclude_account_names": []
    },
    "quota": {
        "default_soft": 10,
        "default_hard": 12
    },
    "session": {
        "db_path": "/var/lib/lab-daemon/state.db",
        "idle_timeout_minutes": 60,
        "unmount_grace_seconds": 10
    },
    "nemo": {
        "django_path": str(Path(__file__).resolve().parent.parent / "nemo-ce"),
        "poll_interval_seconds": 3600,
        "use_api_http": False,
        "api_url": "http://localhost:8000",
        "api_token": "nemo_api_token_here_replace_me"
    },
    "sync": {
        "on_deactivation": "lock_account",
        "dry_run": False
    },
    "mtls": {
        "enabled": False,
        "ca_cert": "certs/ca.crt",
        "server_cert": "certs/server.crt",
        "server_key": "certs/server.key"
    }
}

import copy
config = copy.deepcopy(DEFAULT_CONFIG)

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
                # Merge nested dicts
                for key in ("storage", "quota", "session", "nemo", "sync", "mtls"):
                    if key in user_config and isinstance(user_config[key], dict):
                        config[key].update(user_config[key])
                if "shared_secret" in user_config:
                    config["shared_secret"] = user_config["shared_secret"]
        log.info(f"Loaded configuration from: {config_path}")
    except Exception as e:
        log.error(f"Error loading configuration from {config_path}: {e}")

# Resolve directories
BASE_DIR = Path(config["storage"]["base_path"])
USERS_DIR = Path(config["storage"]["users_path"])
GROUPS_DIR = Path(config["storage"]["groups_path"])
SESSIONS_DIR = Path(config["storage"]["sessions_path"])
PUBLIC_DIR = Path(config["storage"]["public_path"])

# Initialize Database & API
state_db = StateDB(config["session"]["db_path"])
nemo_client = NemoAPIClient(
    django_path=config["nemo"]["django_path"],
    use_api_http=config["nemo"]["use_api_http"],
    api_url=config["nemo"]["api_url"],
    api_token=config["nemo"]["api_token"]
)
user_provisioner = UserProvisioner(
    base_path=config["storage"]["base_path"],
    users_path=config["storage"]["users_path"],
    groups_path=config["storage"]["groups_path"],
    quota_soft_gb=config["quota"]["default_soft"],
    quota_hard_gb=config["quota"]["default_hard"]
)
samba_controller = SambaController(config["storage"]["sessions_path"])

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
_libc_name = ctypes.util.find_library("c")
if _libc_name is None:
    _libc = None
else:
    _libc = ctypes.CDLL(_libc_name, use_errno=True)

MS_BIND = 4096
MS_RDONLY = 1
MS_REMOUNT = 32

def _mount_bind(source: Path, target: Path) -> None:
    target.mkdir(parents=True, exist_ok=True)
    try:
        subprocess.run(["mount", "--bind", str(source), str(target)], check=True, capture_output=True)
    except subprocess.CalledProcessError as e:
        raise OSError(e.returncode, f"mount --bind failed: {e.stderr.decode().strip()}")

def _mount_bind_ro(source: Path, target: Path) -> None:
    target.mkdir(parents=True, exist_ok=True)
    try:
        subprocess.run(["mount", "--bind", str(source), str(target)], check=True, capture_output=True)
        subprocess.run(["mount", "-o", "remount,ro,bind", str(target)], check=True, capture_output=True)
    except subprocess.CalledProcessError as e:
        raise OSError(e.returncode, f"mount RO failed: {e.stderr.decode().strip()}")

def _umount(target: Path) -> None:
    try:
        subprocess.run(["umount", str(target)], check=True, capture_output=True)
    except subprocess.CalledProcessError as e:
        raise OSError(e.returncode, f"umount failed: {e.stderr.decode().strip()}")

def _is_mountpoint(path: Path) -> bool:
    if not path.exists():
        return False
    try:
        target_path = str(path.resolve())
        if os.path.exists("/proc/self/mountinfo"):
            with open("/proc/self/mountinfo", "r") as f:
                for line in f:
                    parts = line.strip().split()
                    if len(parts) > 4:
                        mount_point = parts[4].replace("\\040", " ").replace("\\011", "\t").replace("\\012", "\n").replace("\\134", "\\")
                        if mount_point == target_path:
                            return True
        else:
            return os.path.isdir(path)
    except Exception:
        pass
    return False

def check_open_files(dir_path: Path) -> list:
    try:
        res = subprocess.run(["lsof", "+D", str(dir_path)], capture_output=True, text=True)
        if res.returncode == 0:
            lines = res.stdout.strip().split("\n")[1:]
            open_files = []
            for line in lines:
                parts = line.split()
                if len(parts) > 8:
                    open_files.append(f"PID {parts[1]} ({parts[0]}): {parts[8]}")
            return open_files
    except FileNotFoundError:
        pass
    except Exception as e:
        log.debug(f"Error checking open files: {e}")
    return []

def graceful_unmount(target: Path, grace_seconds: int) -> bool:
    if not _is_mountpoint(target):
        return True
    start_time = time.time()
    open_files = check_open_files(target)
    while open_files and (time.time() - start_time < grace_seconds):
        log.warning(f"⚠️ Directory {target} is busy (files open). Waiting for files to close... ({int(grace_seconds - (time.time() - start_time))}s left)")
        time.sleep(2)
        open_files = check_open_files(target)
    if open_files:
        log.warning(f"🚨 Grace period expired for {target}. Forcing lazy unmount (umount -l).")
        try:
            subprocess.run(["umount", "-l", str(target)], check=True, capture_output=True)
            return True
        except subprocess.CalledProcessError as e:
            log.error(f"❌ Force unmount failed for {target}: {e.stderr.strip()}")
            return False
    else:
        try:
            _umount(target)
            return True
        except OSError as exc:
            log.error(f"❌ Unmount failed for {target}: {exc}")
            return False

def _safe_name(name: str) -> str:
    import re
    if not name:
        return name
    name = re.sub(r'[/\\:\*\?"<>\|]', '_', name)
    name = re.sub(r' +', ' ', name).strip()
    name = name.rstrip('.')
    reserved_patterns = re.compile(r'^(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])$', re.IGNORECASE)
    if reserved_patterns.match(name):
        name = f"_{name}"
    return name if name else "unnamed_folder"

def is_project_excluded(proj_id: int, proj_name: str, account_id: int, account_name: str) -> bool:
    if proj_id in config["storage"].get("exclude_project_ids", []):
        return True
    if account_id in config["storage"].get("exclude_account_ids", []):
        return True
    p_names = [n.lower() for n in config["storage"].get("exclude_project_names", [])]
    if proj_name and proj_name.lower() in p_names:
        return True
    a_names = [n.lower() for n in config["storage"].get("exclude_account_names", [])]
    if account_name and account_name.lower() in a_names:
        return True
    return False

# ---------------------------------------------------------------------------
# Command Handlers
# ---------------------------------------------------------------------------
def handle_mount(payload):
    user_id = payload.get("user_id")
    tool = payload.get("tool")
    account_id = payload.get("account_id")
    project_id = payload.get("project_id")
    session_id = payload.get("session_id") or f"session_{user_id}_{tool}"

    log.info(f"🔑 USER ACTION: User ID {user_id} logging into {tool} (project {project_id} / account {account_id})")

    # DB user check
    user_info = state_db.get_user_by_id(user_id)
    if not user_info:
        nemo_sync.run_once()
        user_info = state_db.get_user_by_id(user_id)
    if not user_info:
        return {"error": f"User ID {user_id} not found in Django/state DB"}, 404

    username = user_info["username"]
    source_user = USERS_DIR / f"u{user_id}"
    source_files = source_user / "files"

    account_info = state_db.get_account_by_id(account_id)
    project_info = state_db.get_project_by_id(project_id)
    if not account_info or not project_info:
        nemo_sync.run_once()
        account_info = state_db.get_account_by_id(account_id)
        project_info = state_db.get_project_by_id(project_id)

    if user_info and not user_info.get("active", True):
        return {"error": "User is deactivated"}, 403
    if account_info and not account_info.get("active", True):
        return {"error": "Account is deactivated"}, 403
    if project_info and not project_info.get("active", True):
        return {"error": "Project is deactivated"}, 403

    account_name = account_info["name"] if account_info else ""
    project_name = project_info["name"] if project_info else ""

    tool_lower = tool.lower()
    group_folder_type = config["storage"].get("group_folder_type", "account")
    target_user = SESSIONS_DIR / tool_lower / "my_files"
    target_groups = SESSIONS_DIR / tool_lower / "my_groups"
    target_public = SESSIONS_DIR / tool_lower / "public"

    # Pre-create standard persistent root tool folders
    target_user.mkdir(parents=True, exist_ok=True)
    target_groups.mkdir(parents=True, exist_ok=True)
    target_public.mkdir(parents=True, exist_ok=True)

    # Fetch ALL accounts/projects this user belongs to
    user_projects = state_db.get_user_projects_with_accounts(user_id)

    # Provision machine account for ACL grants
    tool_machine = user_provisioner.provision_machine_account(tool)

    # Grant ACL access to personal files directory
    source_user.mkdir(parents=True, exist_ok=True)
    source_files.mkdir(parents=True, exist_ok=True)
    acl_manager.grant_acl_access(tool_machine, str(source_files), "rwx")

    mount_points = []
    quota_warning = None

    # Quota check
    quota_info = quota_manager.check_quota_usage(f"u{user_id}", str(USERS_DIR))
    if quota_info.get("exceeded"):
        quota_warning = "Quota exceeded!"

    # Mount user personal files directory
    user_already_mounted = _is_mountpoint(target_user)
    if not user_already_mounted:
        try:
            _mount_bind(source_files, target_user)
        except OSError as exc:
            return {"error": f"User mount failed: {exc}"}, 500
    mount_points.append(f"{source_files} → {target_user}")

    # Create account directories under my_groups (deduplicated, no bind mounts)
    mounted_account_ids = set()
    for p in user_projects:
        p_account_id = p["account_id"]
        p_account_name = p["account_name"]
        p_project_id = p["project_id"]
        p_project_name = p["project_name"]

        if is_project_excluded(p_project_id, p_project_name, p_account_id, p_account_name):
            continue

        acc_fold = _safe_name(p_account_name) if p_account_name else f"account_{p_account_id}"
        proj_fold = _safe_name(p_project_name) if p_project_name else f"project_{p_project_id}"

        source_acc_dir = GROUPS_DIR / f"account_{p_account_id}"
        source_acc_dir.mkdir(parents=True, exist_ok=True)

        if group_folder_type == "account":
            if p_account_id not in mounted_account_ids:
                target_acc = target_groups / acc_fold
                target_acc.mkdir(parents=True, exist_ok=True)
                mounted_account_ids.add(p_account_id)
        elif group_folder_type == "project":
            # Each project gets its own folder
            target_proj = target_groups / proj_fold
            target_proj.mkdir(parents=True, exist_ok=True)
            source_proj_dir = source_acc_dir / f"project_{p_project_id}"
            source_proj_dir.mkdir(parents=True, exist_ok=True)
            acl_manager.grant_acl_access(tool_machine, str(source_acc_dir), "x")
            acl_manager.grant_acl_access(tool_machine, str(source_proj_dir), "rwx")
            if not _is_mountpoint(target_proj):
                try:
                    _mount_bind(source_proj_dir, target_proj)
                    mount_points.append(f"{source_proj_dir} → {target_proj}")
                except OSError as exc:
                    log.error(f"Project mount failed for {proj_fold}: {exc}")
        else:
            # Hierarchical: account/project
            target_acc = target_groups / acc_fold
            target_proj = target_acc / proj_fold
            target_proj.mkdir(parents=True, exist_ok=True)
            source_proj_dir = source_acc_dir / f"project_{p_project_id}"
            source_proj_dir.mkdir(parents=True, exist_ok=True)
            acl_manager.grant_acl_access(tool_machine, str(source_acc_dir), "x")
            acl_manager.grant_acl_access(tool_machine, str(source_proj_dir), "rwx")
            if not _is_mountpoint(target_proj):
                try:
                    _mount_bind(source_proj_dir, target_proj)
                    mount_points.append(f"{source_proj_dir} → {target_proj}")
                except OSError as exc:
                    log.error(f"Project mount failed for {acc_fold}/{proj_fold}: {exc}")

    # Public Mount
    if PUBLIC_DIR.exists() and PUBLIC_DIR.is_dir():
        acl_manager.grant_acl_access(tool_machine, str(PUBLIC_DIR), "rx")
    public_already_mounted = _is_mountpoint(target_public)
    if not public_already_mounted and PUBLIC_DIR.exists() and PUBLIC_DIR.is_dir():
        try:
            _mount_bind_ro(PUBLIC_DIR, target_public)
        except Exception as exc:
            log.error(f"Public mount failed: {exc}")
    mount_points.append(f"{PUBLIC_DIR} → {target_public}")

    state_db.save_session(session_id, user_id, tool, mount_points)

    resp = {
        "status": "already_mounted" if user_already_mounted else "mounted",
        "path": str(target_user),
        "groups_path": str(target_groups),
        "session_id": session_id
    }
    if quota_warning:
        resp["quota_warning"] = quota_warning

    return resp, 201

def handle_unmount(payload):
    user_id = payload.get("user_id")
    tool = payload.get("tool")
    account_id = payload.get("account_id")
    project_id = payload.get("project_id")
    session_id = payload.get("session_id")

    log.info(f"🔓 UNMOUNT REQUEST: User ID {user_id} leaving {tool} (project {project_id} / account {account_id})")

    # Find matching session
    sessions = state_db.get_all_sessions()
    if not session_id:
        for s_id, s_info in sessions.items():
            if s_info.get("user_id") == user_id and s_info.get("tool") == tool:
                session_id = s_id
                break

    # Quota ownership sync: chown user files to u{user_id}:nogroup and project files to root:proj_{project_id}
    # This ensures files written over Samba (which forces user root) are accounted for in user/group quotas.
    try:
        source_user = USERS_DIR / f"u{user_id}"
        if source_user.exists():
            log.info(f"💾 Quota ownership sync: chown -R -h u{user_id}:nogroup for {source_user}")
            subprocess.run(["chown", "-R", "-h", f"u{user_id}:nogroup", str(source_user)], capture_output=True)
    except Exception as e:
        log.error(f"Failed to chown user directory {source_user} for quota update: {e}")

    try:
        if project_id:
            proj_dir = GROUPS_DIR / f"account_{account_id}" / f"project_{project_id}"
            if proj_dir.exists():
                log.info(f"💾 Quota ownership sync: chown -R -h root:proj_{project_id} for {proj_dir}")
                subprocess.run(["chown", "-R", "-h", f"root:proj_{project_id}", str(proj_dir)], capture_output=True)
    except Exception as e:
        log.error(f"Failed to chown project directory {proj_dir} for quota update: {e}")

    tool_lower = tool.lower()
    tool_session_dir = SESSIONS_DIR / tool_lower
    live_mounts = []
    try:
        with open("/proc/mounts", "r") as f:
            for line in f:
                fields = line.split()
                if len(fields) >= 2:
                    mp = fields[1].replace("\\040", " ")
                    if str(tool_session_dir) in mp:
                        live_mounts.append(mp)
        live_mounts.sort(key=len, reverse=True)
    except Exception as e:
        log.error(f"Could not read /proc/mounts: {e}")

    grace_seconds = config["session"]["unmount_grace_seconds"]
    unmount_success = True

    if live_mounts:
        for mp in live_mounts:
            target_path = Path(mp)
            if not graceful_unmount(target_path, grace_seconds):
                unmount_success = False
            time.sleep(0.3)

    # Remove dynamic user project directories bottom-up using Path.rmdir (only deletes if empty)
    # This prevents any recursive deletion of remote user files if an unmount failed.
    my_groups_dir = tool_session_dir / "my_groups"
    if my_groups_dir.exists():
        for root, dirs, files in os.walk(str(my_groups_dir), topdown=False):
            for d in dirs:
                dir_path = Path(root) / d
                if not _is_mountpoint(dir_path):
                    try:
                        dir_path.rmdir()
                    except OSError:
                        pass

    # Revoke permissions for user and public directories
    tool_machine = f"{tool_lower}_machine"
    source_user = USERS_DIR / f"u{user_id}"
    acl_manager.revoke_acl_access(tool_machine, str(source_user))
    acl_manager.revoke_acl_access(tool_machine, str(PUBLIC_DIR))

    # Revoke permissions for all associated accounts/projects
    user_projects = state_db.get_user_projects_with_accounts(user_id)
    revoked_account_ids = set()
    for p in user_projects:
        p_account_id = p["account_id"]
        p_project_id = p["project_id"]
        acc_dir = GROUPS_DIR / f"account_{p_account_id}"
        proj_dir = acc_dir / f"project_{p_project_id}"
        if p_account_id not in revoked_account_ids:
            acl_manager.revoke_acl_access(tool_machine, str(acc_dir))
            revoked_account_ids.add(p_account_id)
        acl_manager.revoke_acl_access(tool_machine, str(proj_dir))

    if session_id:
        state_db.remove_session(session_id)

    if unmount_success:
        return {"status": "unmounted"}, 200
    else:
        return {"error": "Failed to unmount cleanly"}, 500

def handle_init_user(payload):
    user_id = payload.get("user_id")
    if user_id is None:
        return {"error": "'user_id' required"}, 400

    log.info(f"🔐 USER INITIALIZATION REQUEST: User ID {user_id}")

    user_info = state_db.get_user_by_id(user_id)
    if not user_info:
        nemo_sync.run_once()
        user_info = state_db.get_user_by_id(user_id)

    if not user_info:
        user_provisioner.provision_user(user_id, f"user_{user_id}")
    else:
        user_provisioner.provision_user(user_id, user_info["username"])

    user_dir_path = user_provisioner.ensure_user_directory(user_id)
    user_provisioner.apply_user_quota(user_id)

    return {"status": "initialized", "path": user_dir_path}, 200

def handle_sessions(payload):
    return state_db.get_all_sessions(), 200

def handle_projects(payload):
    user_id = payload.get("user_id")
    if not user_id:
        return {"error": "'user_id' required"}, 400
    return state_db.get_project_linux_groups(int(user_id)), 200

def handle_quota(payload):
    username_or_id = payload.get("username_or_id")
    if not username_or_id:
        return {"error": "'username_or_id' required"}, 400

    username = username_or_id
    if username.startswith("u") and username[1:].isdigit():
        user_id = int(username[1:])
    elif username.isdigit():
        user_id = int(username)
    else:
        user_info = state_db.get_user_by_username(username)
        user_id = user_info["id"] if user_info else None

    if user_id is None:
        return {"error": "User not found"}, 404

    quota_info = quota_manager.check_quota_usage(f"u{user_id}", str(USERS_DIR))
    return quota_info, 200

# ---------------------------------------------------------------------------
# Auto-unmount Callback for Idle Session Monitor
# ---------------------------------------------------------------------------
def auto_unmount_session(user_id, tool, session_id):
    log.info(f"⏳ Auto-unmount: Triggered for user ID '{user_id}' on tool '{tool}' (Session: {session_id})")
    session_info = state_db.get_session(session_id)
    if not session_info:
        log.warning(f"⏳ Auto-unmount: Session {session_id} not found in database.")
        return

    mount_points = session_info.get("mount_points", [])
    grace_seconds = config["session"]["unmount_grace_seconds"]
    
    for mp_str in mount_points:
        if "→" in mp_str:
            target_str = mp_str.split("→")[-1].strip()
            target_path = Path(target_str)
            if graceful_unmount(target_path, grace_seconds):
                time.sleep(0.3)
                try:
                    if target_path.exists() and not _is_mountpoint(target_path):
                        target_path.rmdir()
                except OSError as e:
                    log.warning(f"⏳ Auto-unmount: Failed to remove dir {target_path}: {e}")

    # Remove empty dirs
    tool_session_dir = SESSIONS_DIR / tool
    def _rmdir_if_empty(path: Path):
        try:
            if path.exists() and not _is_mountpoint(path) and not list(path.iterdir()):
                path.rmdir()
        except OSError:
            pass

    if tool_session_dir.exists():
        for dirpath, dirnames, filenames in os.walk(str(tool_session_dir), topdown=False):
            _rmdir_if_empty(Path(dirpath))
        _rmdir_if_empty(tool_session_dir)

    # Revoke POSIX ACLs
    acl_manager.revoke_acl_access(f"{tool.lower()}_machine", str(USERS_DIR / f"u{user_id}"))
    acl_manager.revoke_acl_access(f"{tool.lower()}_machine", str(PUBLIC_DIR))
    
    state_db.remove_session(session_id)
    log.info(f"⏳ Auto-unmount completed for user ID '{user_id}' on tool '{tool}'")

# ---------------------------------------------------------------------------
# Background Threads Setup
# ---------------------------------------------------------------------------
nemo_sync = NemoSync(
    api_client=nemo_client,
    db=state_db,
    user_provisioner=user_provisioner,
    on_deactivation=config["sync"]["on_deactivation"],
    poll_interval=config["nemo"]["poll_interval_seconds"],
    dry_run=config["sync"]["dry_run"],
    exclude_project_ids=config["storage"].get("exclude_project_ids", []),
    exclude_account_ids=config["storage"].get("exclude_account_ids", []),
    exclude_project_names=config["storage"].get("exclude_project_names", []),
    exclude_account_names=config["storage"].get("exclude_account_names", [])
)

idle_monitor = IdleMonitor(
    session_manager=state_db,
    unmount_callback=auto_unmount_session,
    idle_timeout_minutes=config["session"]["idle_timeout_minutes"],
    samba_controller=samba_controller
)

# ---------------------------------------------------------------------------
# Flask App Setup
# ---------------------------------------------------------------------------
app = Flask(__name__)

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
    if config.get("mtls", {}).get("enabled"):
        log.warning("🔐 mTLS verification failed: X-Client-Verify header is missing")
        return False
    return True

@app.before_request
def verify_hmac():
    """
    Verify request HMAC-SHA256 signature to protect the daemon control API.
    """
    if request.path == "/health":
        return None

    signature = request.headers.get("X-Daemon-Signature")
    timestamp_str = request.headers.get("X-Daemon-Timestamp")

    if not signature or not timestamp_str:
        log.warning("🔒 HMAC verification failed: Missing X-Daemon-Signature or X-Daemon-Timestamp header")
        return jsonify({"error": "Unauthorized: Missing signature or timestamp"}), 401

    try:
        timestamp = int(timestamp_str)
    except ValueError:
        return jsonify({"error": "Unauthorized: Invalid timestamp format"}), 401

    now = int(time.time())
    if abs(now - timestamp) > 300:
        log.warning(f"🔒 HMAC verification failed: Clock drift too high (client: {timestamp}, server: {now})")
        return jsonify({"error": "Unauthorized: Request expired or clock out of sync"}), 401

    raw_body = request.get_data()
    message = f"{timestamp}:".encode('utf-8') + raw_body
    
    shared_secret = config.get("shared_secret")
    if not shared_secret:
        shared_secret = "00d57012a01b31f8364ebdcda42f05d15c3fd5585c69be1b8cdec1c30caa3af7"

    expected_signature = hmac.new(
        shared_secret.encode('utf-8') if isinstance(shared_secret, str) else shared_secret,
        message,
        hashlib.sha256
    ).hexdigest()

    if not hmac.compare_digest(signature, expected_signature):
        log.warning("🔒 HMAC verification failed: Signature mismatch")
        return jsonify({"error": "Unauthorized: Signature mismatch"}), 401

    return None

def _validate_request():
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

    import re
    if not re.match(r"^[a-zA-Z0-9._-]+$", user_id_str):
        return None, ({"error": "Invalid characters in 'user_id' — must contain only letters, numbers, dots, hyphens, or underscores"}, 400)
    if not re.match(r"^[a-zA-Z0-9_-]+$", tool):
        return None, ({"error": "Invalid characters in 'tool' — must contain only letters, numbers, hyphens, or underscores"}, 400)

    return {
        "user_id": user_id,
        "user_id_str": user_id_str,
        "tool": tool,
        "account_id": account_id_int,
        "project_id": project_id_int,
        "session_id": data.get("session_id")
    }, None

@app.route("/mount", methods=["POST"])
def mount():
    data, err = _validate_request()
    if err:
        return jsonify(err[0]), err[1]

    if config.get("mtls", {}).get("enabled") and not verify_client_auth():
        return jsonify({"error": "Mutual TLS client verification failed"}), 401

    res, code = handle_mount(data)
    return jsonify(res), code

@app.route("/unmount", methods=["POST"])
def unmount():
    data, err = _validate_request()
    if err:
        return jsonify(err[0]), err[1]

    if config.get("mtls", {}).get("enabled") and not verify_client_auth():
        return jsonify({"error": "Mutual TLS client verification failed"}), 401

    res, code = handle_unmount(data)
    return jsonify(res), code

@app.route("/init_user", methods=["POST"])
def init_user():
    data = request.get_json(silent=True)
    if data is None:
        return jsonify({"error": "Invalid JSON"}), 400

    user_id = data.get("user_id")
    if user_id is None:
        return jsonify({"error": "'user_id' required"}), 400

    # Sanitize and validate user_id to prevent path traversal / argument manipulation
    user_id_str = str(user_id).strip()
    import re
    if not re.match(r"^[a-zA-Z0-9._-]+$", user_id_str):
        return jsonify({"error": "Invalid characters in 'user_id'"}), 400

    if config.get("mtls", {}).get("enabled") and not verify_client_auth():
        return jsonify({"error": "Mutual TLS client verification failed"}), 401

    res, code = handle_init_user({"user_id": user_id})
    return jsonify(res), code

@app.route("/sessions", methods=["GET"])
def get_sessions():
    return jsonify(state_db.get_all_sessions()), 200

@app.route("/projects/<int:user_id>", methods=["GET"])
def get_projects(user_id):
    return jsonify(state_db.get_project_linux_groups(user_id)), 200

@app.route("/quota/<username_or_id>", methods=["GET"])
def get_quota(username_or_id):
    res, code = handle_quota({"username_or_id": username_or_id})
    return jsonify(res), code

@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok"}), 200

# ---------------------------------------------------------------------------
# Signal Handling & Startup/Shutdown
# ---------------------------------------------------------------------------
def sigterm_handler(signum, frame):
    log.info("Terminating Daemon...")
    nemo_sync.stop()
    idle_monitor.stop()
    sys.exit(0)

def startup_cleanup():
    """
    On every daemon start, force-unmount ALL bind mounts under SESSIONS_DIR
    and wipe the session state from the DB.
    """
    log.info("🧹 STARTUP CLEANUP: Wiping all stale session mounts...")
    stale_mounts = []
    try:
        with open("/proc/mounts", "r") as f:
            for line in f:
                fields = line.split()
                if len(fields) >= 2:
                    mount_point = fields[1].replace("\\040", " ")
                    if str(SESSIONS_DIR) in mount_point:
                        stale_mounts.append(mount_point)

        stale_mounts.sort(key=len, reverse=True)
        for mp in stale_mounts:
            log.info(f"  Unmounting stale: {mp}")
            subprocess.run(["umount", "-l", mp], capture_output=True)
    except Exception as e:
        log.error(f"Startup cleanup error scanning /proc/mounts: {e}")

    try:
        import shutil
        shutil.rmtree(str(SESSIONS_DIR), ignore_errors=True)
        SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
        log.info(f"  Session directory wiped and recreated: {SESSIONS_DIR}")
    except Exception as e:
        log.error(f"Startup cleanup error wiping sessions dir: {e}")

    cleared = state_db.clear_all_sessions()
    log.info(f"🧹 STARTUP CLEANUP DONE: {len(stale_mounts)} mounts removed, {cleared} DB sessions cleared.")

if __name__ == "__main__":
    if os.name != 'nt' and os.getuid() != 0:
        log.error("Daemon must run as root!")
        sys.exit(1)

    signal.signal(signal.SIGTERM, sigterm_handler)
    signal.signal(signal.SIGINT, sigterm_handler)

    # Ensure directories exist
    USERS_DIR.mkdir(parents=True, exist_ok=True)
    GROUPS_DIR.mkdir(parents=True, exist_ok=True)

    startup_cleanup()

    # Start background loops
    nemo_sync.start()
    idle_monitor.start()

    # Start Flask Web Server
    port = int(os.environ.get("LAB_DAEMON_PORT", 8080))
    host = "127.0.0.1"

    context = None
    if config.get("mtls", {}).get("enabled"):
        import ssl
        mtls_conf = config["mtls"]
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(mtls_conf["server_cert"], mtls_conf["server_key"])
        context.load_verify_locations(mtls_conf["ca_cert"])
        context.verify_mode = ssl.CERT_REQUIRED
        log.info("🔒 mTLS Client-Certificate Verification is ENABLED")

    log.info(f"Monolithic Daemon listening on HTTP/HTTPS at {host}:{port}")
    try:
        app.run(host=host, port=port, debug=False, ssl_context=context)
    finally:
        nemo_sync.stop()
        idle_monitor.stop()
