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
# Color logging (works in terminal)
# ---------------------------------------------------------------------------

class ColorFormatter(logging.Formatter):
    """Add colors to log levels"""
    COLORS = {
        'INFO': '\033[92m',     # Green
        'WARNING': '\033[93m',  # Yellow
        'ERROR': '\033[91m',    # Red
        'MOUNT': '\033[94m',    # Blue for mount
        'UNMOUNT': '\033[95m',  # Magenta for unmount
        'RESET': '\033[0m'
    }
    
    def format(self, record):
        levelname = record.levelname
        if levelname in self.COLORS:
            record.levelname = f"{self.COLORS[levelname]}{levelname}{self.COLORS['RESET']}"
        
        # Special colors for mount/unmount messages
        if 'Mounted' in record.getMessage():
            record.msg = f"{self.COLORS['MOUNT']}🔗 {record.msg}{self.COLORS['RESET']}"
        elif 'Unmounted' in record.getMessage():
            record.msg = f"{self.COLORS['UNMOUNT']}❌ {record.msg}{self.COLORS['RESET']}"
        
        return super().format(record)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

BASE_DIR = Path("/tmp/labdata")
USERS_DIR = BASE_DIR / "users"
SESSIONS_DIR = BASE_DIR / "sessions"

HOST = "0.0.0.0"  # Listen on all interfaces (so Windows can connect via Samba)
PORT = 5000

# ---------------------------------------------------------------------------
# Logging setup
# ---------------------------------------------------------------------------

console_handler = logging.StreamHandler()
console_handler.setFormatter(ColorFormatter('%(asctime)s [%(levelname)s] %(message)s'))

log = logging.getLogger("lab-daemon")
log.setLevel(logging.INFO)
log.addHandler(console_handler)

# Also log to file
#file_handler = logging.FileHandler('/tmp/lab-daemon.log')

#file_handler.setFormatter(logging.Formatter('%(asctime)s [%(levelname)s] %(message)s'))
#log.addHandler(file_handler)

# ---------------------------------------------------------------------------
# Low-level mount helpers (no shell=True)
# ---------------------------------------------------------------------------

_libc_name = ctypes.util.find_library("c")
if _libc_name is None:
    sys.exit("FATAL: cannot locate libc")
_libc = ctypes.CDLL(_libc_name, use_errno=True)

MS_BIND = 4096


def _mount_bind(source: Path, target: Path) -> None:
    """Create a bind mount using mount(2) syscall."""
    # Ensure target parent exists
    target.parent.mkdir(parents=True, exist_ok=True)
    
    ret = _libc.mount(
        str(source).encode(),
        str(target).encode(),
        None,
        MS_BIND,
        None,
    )
    if ret != 0:
        err = ctypes.get_errno()
        raise OSError(err, os.strerror(err))


def _umount(target: Path) -> None:
    """Unmount using umount(2) syscall."""
    ret = _libc.umount(str(target).encode())
    if ret != 0:
        err = ctypes.get_errno()
        raise OSError(err, os.strerror(err))


def _is_mountpoint(path: Path) -> bool:
    """Check if path is a mount point."""
    if not path.exists():
        return False
    try:
        target_path = str(path.resolve())
        with open("/proc/self/mountinfo", "r") as f:
            for line in f:
                parts = line.strip().split()
                if len(parts) > 4 and parts[4] == target_path:
                    return True
    except OSError:
        pass
    return False

# ---------------------------------------------------------------------------
# Request validation
# ---------------------------------------------------------------------------

def _validate_request():
    data = request.get_json(silent=True)
    if data is None:
        return None, ({"error": "Invalid JSON"}, 400)

    user = data.get("user", "").strip()
    tool = data.get("tool", "").strip()

    if not user or not tool:
        return None, ({"error": "'user' and 'tool' required"}, 400)

    for name in (user, tool):
        if "/" in name or "\\" in name or name in (".", ".."):
            return None, ({"error": "Invalid characters"}, 400)

    return data, None

# ---------------------------------------------------------------------------
# Flask app
# ---------------------------------------------------------------------------

app = Flask(__name__)


@app.route("/mount", methods=["POST"])

def mount_endpoint():
    
    data, err = _validate_request()
    if err:
        return jsonify(err[0]), err[1]

    user = data["user"]
    tool = data["tool"]

    source = USERS_DIR / user
    target = SESSIONS_DIR / tool / user

    # Log the user action prominently
    log.info(f"🔐 USER ACTION: {user} logging into {tool}")
    
    # Create directories
    try:
        source.mkdir(parents=True, exist_ok=True)
        target.mkdir(parents=True, exist_ok=True)
        if source.exists() and source.is_dir():
            log.info(f"📁 User directory ready: {source}")
        else:
            log.info(f"📁 Created new user directory: {source}")
    except OSError as exc:
        log.error(f"Failed to create directories: {exc}")
        return jsonify({"error": str(exc)}), 500

    # Check if already mounted
    if _is_mountpoint(target):
        log.info(f"✅ {user} already has active session on {tool}")
        return jsonify({"status": "already_mounted"}), 200

    # Perform mount
    try:
        _mount_bind(source, target)
        log.info(f"✅ MOUNT SUCCESS: {user} → {tool}")
        log.info(f"   Source: {source}")
        log.info(f"   Target: {target}")
        return jsonify({"status": "mounted", "path": str(target)}), 201
    except OSError as exc:
        log.error(f"❌ MOUNT FAILED for {user} on {tool}: {exc}")
        return jsonify({"error": str(exc)}), 500


@app.route("/unmount", methods=["POST"])
def unmount_endpoint():
    data, err = _validate_request()
    if err:
        return jsonify(err[0]), err[1]

    user = data["user"]
    tool = data["tool"]

    target = SESSIONS_DIR / tool / user

    if not target.exists():
        return jsonify({"error": "Not found"}), 404

    if not _is_mountpoint(target):
        return jsonify({"error": "Not a mount point"}), 400

    try:
        _umount(target)
        log.info(f"Unmounted: {user} from {tool} [{target}]")
        
        # Remove the empty directory after unmount
        try:
            target.rmdir()
            log.info(f"Removed empty session directory: {target}")
        except OSError as e:
            log.warning(f"Could not remove {target}: {e}")
        
        return jsonify({"status": "unmounted"}), 200
    except OSError as exc:
        log.error(f"Unmount failed: {exc}")
        return jsonify({"error": str(exc)}), 500

@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok"}), 200


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    if os.geteuid() != 0:
        log.warning("Not running as root — mounts will fail! Run with sudo")
    
    # Ensure base directories exist
    USERS_DIR.mkdir(parents=True, exist_ok=True)
    SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
    
    log.info(f"Starting Lab Data Mount Daemon on {HOST}:{PORT}")
    log.info(f"Users directory: {USERS_DIR}")
    log.info(f"Sessions directory: {SESSIONS_DIR}")
    
    app.run(host=HOST, port=PORT, debug=False)