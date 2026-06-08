#!/usr/bin/env python3
"""
Lab Data Mount Daemon
=====================
A Flask daemon that manages bind mounts for lab instrument sessions.

Exposes two POST endpoints:
  /mount   — bind-mounts a user's data directory into a tool session path
  /unmount — removes that bind mount

Designed to run on WSL2 (Ubuntu 24.04) with Python 3.11+.
Requires root privileges for mount/umount operations.
"""

import ctypes
import ctypes.util
import errno
import json
import logging
import os
import sys
from pathlib import Path

from flask import Flask, jsonify, request

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

BASE_DIR = Path("/tmp/labdata")
USERS_DIR = BASE_DIR / "users"       # Permanent per-user data directories
SESSIONS_DIR = BASE_DIR / "sessions"  # Mount points exposed to Samba

HOST = "127.0.0.1"
PORT = 5000

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("lab-daemon")

# ---------------------------------------------------------------------------
# Low-level mount helpers (no shell=True)
# ---------------------------------------------------------------------------

# Load libc for mount(2) / umount(2) syscalls
_libc_name = ctypes.util.find_library("c")
if _libc_name is None:
    sys.exit("FATAL: cannot locate libc")
_libc = ctypes.CDLL(_libc_name, use_errno=True)

# mount(2) flags
MS_BIND = 4096


def _mount_bind(source: Path, target: Path) -> None:
    """
    Create a bind mount from *source* to *target* using the mount(2) syscall.

    Raises OSError on failure.
    """
    ret = _libc.mount(
        str(source).encode(),   # source
        str(target).encode(),   # target
        None,                   # filesystemtype (ignored for bind)
        MS_BIND,                # mountflags
        None,                   # data
    )
    if ret != 0:
        err = ctypes.get_errno()
        raise OSError(err, os.strerror(err))


def _umount(target: Path) -> None:
    """
    Unmount *target* using the umount(2) syscall.

    Raises OSError on failure.
    """
    ret = _libc.umount(str(target).encode())
    if ret != 0:
        err = ctypes.get_errno()
        raise OSError(err, os.strerror(err))


def _is_mountpoint(path: Path) -> bool:
    """
    Check whether *path* is currently a mount point by parsing /proc/self/mountinfo.
    Returns False if the path does not exist.
    """
    if not path.exists():
        return False
    try:
        target_path = str(path.resolve())
        with open("/proc/self/mountinfo", "r") as f:
            for line in f:
                parts = line.strip().split()
                if len(parts) > 4:
                    if parts[4] == target_path:
                        return True
    except OSError:
        pass
    return False

# ---------------------------------------------------------------------------
# Request validation
# ---------------------------------------------------------------------------


def _validate_request() -> tuple[dict | None, tuple | None]:
    """
    Parse and validate the incoming JSON body.

    Returns (payload, None) on success or (None, error_response_tuple) on
    failure.  The error tuple is (response_dict, http_status_code).
    """
    data = request.get_json(silent=True)
    if data is None:
        return None, (
            {"status": "error", "message": "Request body must be valid JSON"},
            400,
        )

    user = data.get("user", "").strip()
    tool = data.get("tool", "").strip()

    if not user or not tool:
        return None, (
            {"status": "error", "message": "'user' and 'tool' are required"},
            400,
        )

    # Guard against path traversal
    for name in (user, tool):
        if "/" in name or "\\" in name or name in (".", ".."):
            return None, (
                {"status": "error", "message": "Invalid characters in user/tool name"},
                400,
            )

    return data, None

# ---------------------------------------------------------------------------
# Flask application
# ---------------------------------------------------------------------------

app = Flask(__name__)


@app.route("/mount", methods=["POST"])
def mount_endpoint():
    """
    Bind-mount a user's data directory into a tool session path.

    POST /mount
    Body: {"user": "<username>", "tool": "<toolname>"}

    Creates:
      source: /tmp/labdata/users/<user>
      target: /tmp/labdata/sessions/<tool>/<user>

    Both directories are created if they do not already exist.
    """
    data, err = _validate_request()
    if err is not None:
        return jsonify(err[0]), err[1]

    user = data["user"].strip()
    tool = data["tool"].strip()

    source = USERS_DIR / user
    target = SESSIONS_DIR / tool / user

    # Create directories if they don't exist
    try:
        source.mkdir(parents=True, exist_ok=True)
        target.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        log.error("Failed to create directories: %s", exc)
        return jsonify({"status": "error", "message": f"Directory creation failed: {exc}"}), 500

    # Check if already mounted
    if _is_mountpoint(target):
        log.info("Already mounted: %s -> %s", source, target)
        return jsonify({
            "status": "ok",
            "message": "Already mounted",
            "source": str(source),
            "target": str(target),
        }), 200

    # Perform the bind mount
    try:
        _mount_bind(source, target)
    except OSError as exc:
        log.error("mount(%s, %s) failed: %s", source, target, exc)
        return jsonify({"status": "error", "message": f"Mount failed: {exc}"}), 500

    log.info("Mounted: %s -> %s", source, target)
    return jsonify({
        "status": "ok",
        "message": "Bind mount created",
        "source": str(source),
        "target": str(target),
    }), 201


@app.route("/unmount", methods=["POST"])
def unmount_endpoint():
    """
    Unmount a previously created bind mount for a tool session.

    POST /unmount
    Body: {"user": "<username>", "tool": "<toolname>"}
    """
    data, err = _validate_request()
    if err is not None:
        return jsonify(err[0]), err[1]

    user = data["user"].strip()
    tool = data["tool"].strip()

    target = SESSIONS_DIR / tool / user

    # Check that the target exists and is a mount point
    if not target.exists():
        return jsonify({
            "status": "error",
            "message": f"Target path does not exist: {target}",
        }), 404

    if not _is_mountpoint(target):
        return jsonify({
            "status": "error",
            "message": f"Target is not a mount point: {target}",
        }), 400

    # Perform the unmount
    try:
        _umount(target)
    except OSError as exc:
        log.error("umount(%s) failed: %s", target, exc)
        return jsonify({"status": "error", "message": f"Unmount failed: {exc}"}), 500

    log.info("Unmounted: %s", target)
    return jsonify({
        "status": "ok",
        "message": "Bind mount removed",
        "target": str(target),
    }), 200


# ---------------------------------------------------------------------------
# Health check
# ---------------------------------------------------------------------------

@app.route("/health", methods=["GET"])
def health():
    """Simple liveness probe."""
    return jsonify({"status": "ok"}), 200


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    # Warn (but don't block) if not running as root
    if os.geteuid() != 0:
        log.warning(
            "Not running as root — mount/umount operations will fail. "
            "Re-run with: sudo python3 %s",
            sys.argv[0],
        )

    log.info("Starting Lab Data Mount Daemon on %s:%d", HOST, PORT)
    app.run(host=HOST, port=PORT, debug=False)
