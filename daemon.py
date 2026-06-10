#!/usr/bin/env python3
"""
Lab Data Mount Daemon
=====================
A Flask daemon that manages bind mounts for lab instrument sessions.

Exposes endpoints:
  /mount   — bind-mounts user and group directories into a tool session path (HMAC signed)
  /unmount — removes bind mounts gracefully, waiting if files are open (HMAC signed)
  /sessions — lists all active sessions
  /quota/<username> — lists disk quota usage for the specified user
  /health  — health check endpoint

Designed to run on WSL2 (Ubuntu 24.04) with Python 3.11+.
Requires root privileges for mount/umount and ACL/quota operations.
"""

import ctypes
import ctypes.util
import errno
import hashlib
import hmac
import json
import logging
import os
import subprocess
import sys
import time
from pathlib import Path

import yaml
from flask import Flask, jsonify, request

# Import custom managers
from session_state import SessionStateManager
import acl_manager
import quota_manager
from idle_monitor import IdleMonitor

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
        if 'Mounted' in record.getMessage() or 'MOUNT SUCCESS' in record.getMessage():
            record.msg = f"{self.COLORS['MOUNT']}🔗 {record.msg}{self.COLORS['RESET']}"
        elif 'Unmounted' in record.getMessage() or 'Unmounting' in record.getMessage():
            record.msg = f"{self.COLORS['UNMOUNT']}❌ {record.msg}{self.COLORS['RESET']}"
        
        return super().format(record)

# ---------------------------------------------------------------------------
# Logging setup
# ---------------------------------------------------------------------------

console_handler = logging.StreamHandler()
console_handler.setFormatter(ColorFormatter('%(asctime)s [%(levelname)s] %(message)s'))

log = logging.getLogger("lab-daemon")
log.setLevel(logging.INFO)
log.addHandler(console_handler)

# ---------------------------------------------------------------------------
# Configuration loading
# ---------------------------------------------------------------------------

DEFAULT_CONFIG = {
    "storage": {
        "base_path": "/srv/labdata",
        "users_path": "/srv/labdata/users",
        "groups_path": "/srv/labdata/groups",
        "sessions_path": "/tmp/labdata/sessions"
    },
    "quota": {
        "default_soft": 10,  # GB
        "default_hard": 12   # GB
    },
    "session": {
        "db_path": "/var/lib/lab-daemon/sessions.json",
        "idle_timeout_minutes": 60,
        "unmount_grace_seconds": 30
    },
    "group_mapping": {
        "alice": "cleanroom",
        "bob": "cleanroom",
        "charlie": "metrology",
        "admin": "staff"
    }
}

config = DEFAULT_CONFIG.copy()

# Look for config.yaml in standard locations
config_locations = [
    os.environ.get("LAB_DAEMON_CONFIG"),
    "/etc/lab-daemon/config.yaml",
    "config.yaml",
    os.path.join(os.path.dirname(__file__), "config.yaml") if "__file__" in locals() else None
]

for loc in config_locations:
    if loc and os.path.exists(loc):
        try:
            with open(loc, "r") as f:
                loaded = yaml.safe_load(f)
                if loaded:
                    # Deep merge configuration
                    for section in ["storage", "quota", "session"]:
                        if section in loaded and isinstance(loaded[section], dict):
                            config[section].update(loaded[section])
                    if "group_mapping" in loaded and isinstance(loaded["group_mapping"], dict):
                        config["group_mapping"].update(loaded["group_mapping"])
            log.info(f"Loaded configuration from: {loc}")
            break
        except Exception as e:
            log.error(f"Error loading configuration from {loc}: {e}")

BASE_DIR = Path(config["storage"]["base_path"])
USERS_DIR = Path(config["storage"]["users_path"])
GROUPS_DIR = Path(config["storage"]["groups_path"])
SESSIONS_DIR = Path(config["storage"]["sessions_path"])

SECRET_KEY = b"00d57012a01b31f8364ebdcda42f05d15c3fd5585c69be1b8cdec1c30caa3af7"
HOST = "0.0.0.0"  # Listen on all interfaces
PORT = 5000

# Instantiate session manager
session_manager = SessionStateManager(config["session"]["db_path"])

# ---------------------------------------------------------------------------
# Low-level mount helpers (no shell=True)
# ---------------------------------------------------------------------------

_libc_name = ctypes.util.find_library("c")
if _libc_name is None:
    # Fallback to load on non-Unix environments (for tests)
    log.warning("Could not locate libc (might be on non-Unix system)")
    _libc = None
else:
    _libc = ctypes.CDLL(_libc_name, use_errno=True)

MS_BIND = 4096


def _mount_bind(source: Path, target: Path) -> None:
    """Create a bind mount using mount(2) syscall."""
    target.parent.mkdir(parents=True, exist_ok=True)
    
    if not _libc:
        log.warning(f"Mocking Bind Mount: {source} → {target}")
        return

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
    if not _libc:
        log.warning(f"Mocking Unmount: {target}")
        return

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
        if os.path.exists("/proc/self/mountinfo"):
            with open("/proc/self/mountinfo", "r") as f:
                for line in f:
                    parts = line.strip().split()
                    if len(parts) > 4 and parts[4] == target_path:
                        return True
        else:
            # Fallback/mock check for tests
            return os.path.isdir(path)
    except OSError:
        pass
    return False

# ---------------------------------------------------------------------------
# Graceful Unmount & Open File Handling helpers
# ---------------------------------------------------------------------------

def check_open_files(dir_path: Path) -> list:
    """
    Checks if any files are currently open under the directory path using lsof.
    """
    try:
        res = subprocess.run(["lsof", "+D", str(dir_path)], capture_output=True, text=True)
        if res.returncode == 0:
            lines = res.stdout.strip().split("\n")[1:] # Skip header
            open_files = []
            for line in lines:
                parts = line.split()
                if len(parts) > 8:
                    open_files.append(f"PID {parts[1]} ({parts[0]}): {parts[8]}")
            return open_files
    except FileNotFoundError:
        # lsof not installed, fallback
        pass
    except Exception as e:
        log.debug(f"Error checking open files: {e}")
    return []


def graceful_unmount(target: Path, grace_seconds: int) -> bool:
    """
    Attempts standard unmount. If files are open, waits up to grace_seconds,
    then forces unmount using umount -l (lazy unmount).
    """
    if not _is_mountpoint(target):
        return True

    start_time = time.time()
    open_files = check_open_files(target)
    
    while open_files and (time.time() - start_time < grace_seconds):
        log.warning(f"⚠️ Directory {target} is busy (files open). Waiting for files to close... ({int(grace_seconds - (time.time() - start_time))}s left)")
        time.sleep(2)
        open_files = check_open_files(target)

    if open_files:
        log.warning(f"🚨 Grace period expired for {target}. Open files: {open_files}. Forcing lazy unmount (umount -l).")
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
            log.warning(f"⚠️ Standard unmount failed for {target}: {exc}. Attempting lazy unmount.")
            try:
                subprocess.run(["umount", "-l", str(target)], check=True, capture_output=True)
                return True
            except Exception as le:
                log.error(f"❌ Lazy unmount failed: {le}")
                return False

# ---------------------------------------------------------------------------
# Request validation & HMAC verification
# ---------------------------------------------------------------------------

def verify_hmac(user: str, tool: str):
    """
    Verify the HMAC-SHA256 signature in request headers.
    Returns (Response, status_code) on failure, or None on success.
    """
    signature = request.headers.get("X-Signature")
    timestamp = request.headers.get("X-Timestamp")

    if not signature or not timestamp:
        log.warning("🔐 HMAC verification failed: Missing X-Signature or X-Timestamp headers")
        return jsonify({"error": "Missing HMAC signature or timestamp headers"}), 401

    try:
        timestamp_val = int(timestamp)
    except (ValueError, TypeError):
        log.warning(f"🔐 HMAC verification failed: Invalid timestamp format '{timestamp}'")
        return jsonify({"error": "Invalid timestamp format"}), 401

    current_time = int(time.time())
    if abs(current_time - timestamp_val) > 30:
        log.warning(f"🔐 HMAC verification failed: Expired timestamp {timestamp_val} (diff: {abs(current_time - timestamp_val)}s)")
        return jsonify({"error": "Request timestamp expired or invalid"}), 401

    message = f"{user}{tool}{timestamp}"
    expected_sig = hmac.new(SECRET_KEY, message.encode("utf-8"), hashlib.sha256).hexdigest()

    if not hmac.compare_digest(expected_sig, signature):
        log.warning("🔐 HMAC verification failed: Invalid signature")
        return jsonify({"error": "Invalid signature"}), 401

    return None


def _validate_request():
    data = request.get_json(silent=True)
    if data is None:
        return None, ({"error": "Invalid JSON"}, 400)

    user = data.get("user", "").strip()
    tool = data.get("tool", "").strip()
    group = data.get("group", "").strip() if "group" in data else None

    if not user or not tool:
        return None, ({"error": "'user' and 'tool' required"}, 400)

    for name in (user, tool):
        if "/" in name or "\\" in name or name in (".", ".."):
            return None, ({"error": "Invalid characters"}, 400)

    if group and ("/" in group or "\\" in group or group in (".", "..")):
        return None, ({"error": "Invalid characters in group"}, 400)

    return data, None

# ---------------------------------------------------------------------------
# Flask App & Endpoints
# ---------------------------------------------------------------------------

app = Flask(__name__)


@app.route("/mount", methods=["POST"])
def mount_endpoint():
    data, err = _validate_request()
    if err:
        return jsonify(err[0]), err[1]

    user = data["user"]
    tool = data["tool"]
    group = data.get("group")
    session_id = data.get("session_id", f"session_{user}_{tool}")

    auth_err = verify_hmac(user, tool)
    if auth_err:
        return auth_err

    # Log the user action prominently
    log.info(f"🔐 USER ACTION: {user} logging into {tool}")

    # Fallback to local group mapping if not provided in payload
    if not group:
        group = config["group_mapping"].get(user)

    # Check user disk quota before mounting
    quota_info = quota_manager.check_quota_usage(user, USERS_DIR)
    quota_warning = None
    if quota_info.get("exceeded"):
        log.warning(f"⚠️ QUOTA WARNING: User '{user}' has exceeded their disk quota limit! (Used: {quota_info.get('used_gb')}GB)")
        quota_warning = "Quota exceeded!"

    source_user = USERS_DIR / user
    target_user = SESSIONS_DIR / tool / user

    # Create directories
    try:
        source_user.mkdir(parents=True, exist_ok=True)
        target_user.mkdir(parents=True, exist_ok=True)
        if source_user.exists() and source_user.is_dir():
            log.info(f"📁 User directory ready: {source_user}")
        else:
            log.info(f"📁 Created new user directory: {source_user}")
    except OSError as exc:
        log.error(f"Failed to create user directories: {exc}")
        return jsonify({"error": str(exc)}), 500

    # Apply default disk quota
    quota_manager.apply_quota(
        user,
        config["quota"]["default_soft"],
        config["quota"]["default_hard"],
        USERS_DIR
    )

    mount_points = []

    # Perform user bind mount
    user_already_mounted = _is_mountpoint(target_user)
    if user_already_mounted:
        log.info(f"✅ {user} already has active user session mount on {tool}")
    else:
        try:
            _mount_bind(source_user, target_user)
            log.info(f"✅ MOUNT SUCCESS: {user} → {tool}")
            log.info(f"   Source: {source_user}")
            log.info(f"   Target: {target_user}")
        except OSError as exc:
            log.error(f"❌ MOUNT FAILED for {user} on {tool}: {exc}")
            return jsonify({"error": str(exc)}), 500

    mount_points.append(f"{source_user} → {target_user}")

    # Perform group mount if group is specified
    if group:
        source_group = GROUPS_DIR / group
        target_group = SESSIONS_DIR / tool / group

        try:
            source_group.mkdir(parents=True, exist_ok=True)
            target_group.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            log.error(f"Failed to create group directories: {exc}")
        else:
            # Grant POSIX ACL access
            acl_manager.grant_group_access(user, source_group)

            # Bind mount group directory
            group_already_mounted = _is_mountpoint(target_group)
            if group_already_mounted:
                log.info(f"✅ Group '{group}' already mounted on {tool}")
            else:
                try:
                    _mount_bind(source_group, target_group)
                    log.info(f"✅ MOUNT SUCCESS (GROUP): {group} → {tool}")
                    log.info(f"   Source: {source_group}")
                    log.info(f"   Target: {target_group}")
                except OSError as exc:
                    log.error(f"❌ MOUNT FAILED for group {group} on {tool}: {exc}")

            mount_points.append(f"{source_group} → {target_group}")

    # Save session state to database
    session_manager.save_session(session_id, user, tool, mount_points)

    response_body = {
        "status": "mounted",
        "path": str(target_user),
        "session_id": session_id
    }
    if quota_warning:
        response_body["quota_warning"] = quota_warning

    status_code = 200 if user_already_mounted else 201
    return jsonify(response_body), status_code


@app.route("/unmount", methods=["POST"])
def unmount_endpoint():
    data, err = _validate_request()
    if err:
        return jsonify(err[0]), err[1]

    user = data["user"]
    tool = data["tool"]
    session_id = data.get("session_id")

    auth_err = verify_hmac(user, tool)
    if auth_err:
        return auth_err

    # Look up session ID in DB if not passed
    sessions = session_manager.get_all_sessions()
    if not session_id:
        for s_id, s_info in sessions.items():
            if s_info.get("user") == user and s_info.get("tool") == tool:
                session_id = s_id
                break

    session_info = sessions.get(session_id) if session_id else None
    mount_points = []
    group = data.get("group")

    if session_info:
        mount_points = session_info.get("mount_points", [])
        if not group:
            # Try to extract group name from mount points
            for mp_str in mount_points:
                if str(GROUPS_DIR) in mp_str:
                    parts = mp_str.split("→")
                    if parts:
                        src_part = parts[0].strip()
                        group = Path(src_part).name
                        break

    if not group:
        group = config["group_mapping"].get(user)

    grace_seconds = config["session"]["unmount_grace_seconds"]
    unmount_success = True

    # Perform unmount for each tracked mount point
    if mount_points:
        for mp_str in mount_points:
            if "→" in mp_str:
                target_str = mp_str.split("→")[-1].strip()
                target_path = Path(target_str)
                
                log.info(f"Unmounting: {target_path}")
                if _is_mountpoint(target_path):
                    if not graceful_unmount(target_path, grace_seconds):
                        unmount_success = False

                # Remove empty session directory
                try:
                    if target_path.exists():
                        target_path.rmdir()
                        log.info(f"Removed empty session directory: {target_path}")
                except OSError as e:
                    log.warning(f"Could not remove {target_path}: {e}")
    else:
        # Fallback to defaults if no DB entry exists
        target_user = SESSIONS_DIR / tool / user
        log.info(f"Unmounting (Fallback User): {target_user}")
        if _is_mountpoint(target_user):
            if not graceful_unmount(target_user, grace_seconds):
                unmount_success = False
        try:
            if target_user.exists():
                target_user.rmdir()
                log.info(f"Removed empty session directory: {target_user}")
        except OSError as e:
            log.warning(f"Could not remove {target_user}: {e}")

        if group:
            target_group = SESSIONS_DIR / tool / group
            log.info(f"Unmounting (Fallback Group): {target_group}")
            if _is_mountpoint(target_group):
                if not graceful_unmount(target_group, grace_seconds):
                    unmount_success = False
            try:
                if target_group.exists():
                    target_group.rmdir()
                    log.info(f"Removed empty session directory: {target_group}")
            except OSError as e:
                log.warning(f"Could not remove {target_group}: {e}")

    # Remove empty tool directory
    tool_dir = SESSIONS_DIR / tool
    try:
        if tool_dir.exists() and not os.listdir(tool_dir):
            tool_dir.rmdir()
            log.info(f"Removed empty tool directory: {tool_dir}")
    except OSError:
        pass

    # Revoke group ACL permissions if group was associated
    if group:
        source_group = GROUPS_DIR / group
        acl_manager.revoke_group_access(user, source_group)

    # Remove session state from database
    if session_id:
        session_manager.remove_session(session_id)

    if unmount_success:
        return jsonify({"status": "unmounted"}), 200
    else:
        return jsonify({"error": "Failed to unmount cleanly"}), 500


@app.route("/sessions", methods=["GET"])
def get_sessions():
    """
    Returns all active sessions in the database.
    """
    return jsonify(session_manager.get_all_sessions()), 200


@app.route("/quota/<username>", methods=["GET"])
def get_user_quota(username):
    """
    Queries and returns quota limits and usage for the user.
    """
    quota_info = quota_manager.check_quota_usage(username, USERS_DIR)
    return jsonify(quota_info), 200


@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok"}), 200

# ---------------------------------------------------------------------------
# Auto-unmount Callback for Idle Session Monitor
# ---------------------------------------------------------------------------

def auto_unmount_session(user, tool, session_id):
    """
    Function triggered by IdleMonitor background thread to clean up idle sessions.
    """
    log.info(f"⏳ Auto-unmount: Triggered for user '{user}' on tool '{tool}' (Session: {session_id})")
    
    sessions = session_manager.get_all_sessions()
    session_info = sessions.get(session_id)
    if not session_info:
        log.warning(f"⏳ Auto-unmount: Session {session_id} not found in database.")
        return

    mount_points = session_info.get("mount_points", [])
    grace_seconds = config["session"]["unmount_grace_seconds"]
    
    for mp_str in mount_points:
        if "→" in mp_str:
            target_str = mp_str.split("→")[-1].strip()
            target_path = Path(target_str)
            
            log.info(f"⏳ Auto-unmount: Unmounting target directory {target_path}")
            if _is_mountpoint(target_path):
                graceful_unmount(target_path, grace_seconds)
            
            try:
                if target_path.exists():
                    target_path.rmdir()
                    log.info(f"Removed empty session directory: {target_path}")
            except OSError as e:
                log.warning(f"Could not remove {target_path}: {e}")

    # Remove tool directory if empty
    tool_dir = SESSIONS_DIR / tool
    try:
        if tool_dir.exists() and not os.listdir(tool_dir):
            tool_dir.rmdir()
            log.info(f"Removed empty tool directory: {tool_dir}")
    except OSError:
        pass

    # Revoke group ACL permissions if group was associated
    group = None
    for mp_str in mount_points:
        if str(GROUPS_DIR) in mp_str:
            parts = mp_str.split("→")
            if parts:
                src_part = parts[0].strip()
                group = Path(src_part).name
                break
    
    if group:
        source_group = GROUPS_DIR / group
        acl_manager.revoke_group_access(user, source_group)

    # Clean up from database
    session_manager.remove_session(session_id)
    log.info(f"⏳ Auto-unmount complete: Session '{session_id}' unmounted successfully")

# ---------------------------------------------------------------------------
# Orphan Recovery
# ---------------------------------------------------------------------------

def recover_orphans(session_mgr, sessions_dir: Path):
    """
    Compares active system mounts under sessions_dir with sessions database
    and lazy-unmounts any orphaned mounts.
    """
    log.info("🧹 Startup: Starting orphan recovery scan...")
    
    sessions_dir_normalized = os.path.normpath(str(sessions_dir))
    system_mounts = []
    try:
        if os.path.exists("/proc/self/mountinfo"):
            with open("/proc/self/mountinfo", "r") as f:
                for line in f:
                    parts = line.strip().split()
                    if len(parts) > 4:
                        mp = os.path.normpath(parts[4])
                        if mp.startswith(sessions_dir_normalized):
                            system_mounts.append(mp)
    except Exception as e:
        log.error(f"Error scanning mountinfo: {e}")
        return

    # Extract active mounts from database
    active_sessions = session_mgr.get_all_sessions()
    active_targets = set()
    for s_id, s_info in active_sessions.items():
        for mp_str in s_info.get("mount_points", []):
            if "→" in mp_str:
                active_targets.add(os.path.normpath(mp_str.split("→")[-1].strip()))

    # Detect and clean orphans
    orphans_cleaned = 0
    for mp in system_mounts:
        if mp not in active_targets:
            log.warning(f"🧹 Orphan recovery: Found orphaned mount point '{mp}'")
            try:
                # Lazy unmount
                subprocess.run(["umount", "-l", mp], check=True, capture_output=True)
                log.info(f"🧹 Orphan recovery: Successfully unmounted '{mp}'")
                orphans_cleaned += 1
                
                # Try to clean up directory
                try:
                    os.rmdir(mp)
                    log.info(f"🧹 Orphan recovery: Removed empty session directory '{mp}'")
                except Exception:
                    pass
            except Exception as e:
                log.error(f"🧹 Orphan recovery: Failed to unmount '{mp}': {e}")
    
    log.info(f"🧹 Orphan recovery completed. Cleaned {orphans_cleaned} orphaned mounts.")

# ---------------------------------------------------------------------------
# Main Entry Point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    if os.name != 'nt' and os.geteuid() != 0:
        log.warning("Not running as root — mounts and ACLs will fail! Run with sudo")
    
    # Ensure base storage and session directories exist
    USERS_DIR.mkdir(parents=True, exist_ok=True)
    GROUPS_DIR.mkdir(parents=True, exist_ok=True)
    SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
    
    # Run orphan recovery
    recover_orphans(session_manager, SESSIONS_DIR)

    # Start idle session monitor background thread
    idle_monitor = IdleMonitor(
        session_manager=session_manager,
        unmount_callback=auto_unmount_session,
        idle_timeout_minutes=config["session"]["idle_timeout_minutes"],
        check_interval_seconds=30  # Poll every 30 seconds for active testing
    )
    idle_monitor.start()

    log.info(f"Starting Lab Data Mount Daemon on {HOST}:{PORT}")
    log.info(f"Users directory: {USERS_DIR}")
    log.info(f"Groups directory: {GROUPS_DIR}")
    log.info(f"Sessions directory: {SESSIONS_DIR}")
    
    try:
        app.run(host=HOST, port=PORT, debug=False)
    finally:
        idle_monitor.stop()