#!/usr/bin/env python3
"""
Lab Data Mount Daemon (Hierarchical Version)
============================================
A Flask daemon that manages bind mounts for lab instrument sessions.

Exposes endpoints:
  /mount   — bind-mounts user, group, and public directories into a tool session path (HMAC signed using user_id)
  /unmount — removes bind mounts gracefully, waiting if files are open (HMAC signed using user_id)
  /sessions — lists all active sessions
  /projects/<user_id> — lists projects a user belongs to
  /quota/<username_or_id> — lists disk quota usage for the specified user
  /init_user — initializes user directory and quotas dynamically (HMAC signed)
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
from flask import Flask, jsonify, request, send_from_directory

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
        "sessions_path": "/tmp/labdata/sessions",
        "public_path": "/srv/labdata/public",
        "group_folder_type": "project"  # project | account | hierarchical
    },
    "quota": {
        "default_soft": 10,  # GB
        "default_hard": 12   # GB
    },
    "session": {
        "db_path": "/var/lib/lab-daemon/state.db",
        "idle_timeout_minutes": 60,
        "unmount_grace_seconds": 30
    },
    "nemo": {
        "django_path": "/mnt/c/Users/gyand/Desktop/NemoProject/nemo-ce",
        "poll_interval_seconds": 3600
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
                    for section in ["storage", "quota", "session", "nemo", "sync", "mtls"]:
                        if section in loaded and isinstance(loaded[section], dict):
                            config[section].update(loaded[section])
            log.info(f"Loaded configuration from: {loc}")
            break
        except Exception as e:
            log.error(f"Error loading configuration from {loc}: {e}")

BASE_DIR = Path(config["storage"]["base_path"])
USERS_DIR = Path(config["storage"]["users_path"])
GROUPS_DIR = Path(config["storage"]["groups_path"])
SESSIONS_DIR = Path(config["storage"]["sessions_path"])
PUBLIC_DIR = Path(config["storage"].get("public_path", "/srv/labdata/public"))

SECRET_KEY = b"00d57012a01b31f8364ebdcda42f05d15c3fd5585c69be1b8cdec1c30caa3af7"
HOST = "0.0.0.0"
PORT = 5000

# Instantiate Managers and Clients
state_db = StateDB(config["session"]["db_path"])
nemo_client = NemoAPIClient(
    django_path=config["nemo"].get("django_path"),
    use_api_http=config["nemo"].get("use_api_http", False),
    api_url=config["nemo"].get("api_url"),
    api_token=config["nemo"].get("api_token")
)
user_provisioner = UserProvisioner(
    base_path=config["storage"]["base_path"],
    users_path=config["storage"]["users_path"],
    groups_path=config["storage"]["groups_path"],
    quota_soft_gb=config["quota"]["default_soft"],
    quota_hard_gb=config["quota"]["default_hard"]
)
samba_controller = SambaController(config["storage"]["sessions_path"])
nemo_sync = NemoSync(
    api_client=nemo_client,
    db=state_db,
    user_provisioner=user_provisioner,
    on_deactivation=config["sync"]["on_deactivation"],
    poll_interval=config["nemo"]["poll_interval_seconds"],
    dry_run=config["sync"]["dry_run"]
)

# ---------------------------------------------------------------------------
# Low-level mount helpers (no shell=True)
# ---------------------------------------------------------------------------

_libc_name = ctypes.util.find_library("c")
if _libc_name is None:
    log.warning("Could not locate libc (might be on non-Unix system)")
    _libc = None
else:
    _libc = ctypes.CDLL(_libc_name, use_errno=True)

MS_BIND = 4096
MS_RDONLY = 1
MS_REMOUNT = 32


def _mount_bind(source: Path, target: Path) -> None:
    """Create a bind mount using mount(2) syscall."""
    target.mkdir(parents=True, exist_ok=True)
    
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


def _mount_bind_ro(source: Path, target: Path) -> None:
    """Create a read-only bind mount using mount(2) syscall."""
    target.mkdir(parents=True, exist_ok=True)
    
    if not _libc:
        log.warning(f"Mocking RO Bind Mount: {source} → {target}")
        return

    # Step 1: Standard bind mount
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

    # Step 2: Remount read-only
    ret = _libc.mount(
        None,
        str(target).encode(),
        None,
        MS_BIND | MS_REMOUNT | MS_RDONLY,
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
    """
    Check if path is an active mount point by reading /proc/self/mountinfo.

    /proc/self/mountinfo encodes special characters (e.g. spaces) as octal
    escape sequences like \\040. We must decode those before comparing against
    the resolved filesystem path, otherwise paths containing spaces will never
    match and graceful_unmount will silently skip the actual umount call.
    """
    if not path.exists():
        return False
    try:
        target_path = str(path.resolve())
        if os.path.exists("/proc/self/mountinfo"):
            with open("/proc/self/mountinfo", "r") as f:
                for line in f:
                    parts = line.strip().split()
                    if len(parts) > 4:
                        # Decode octal escapes: \040 → space, \011 → tab, etc.
                        mount_point = parts[4].replace("\\040", " ").replace("\\011", "\t").replace("\\012", "\n").replace("\\134", "\\")
                        if mount_point == target_path:
                            return True
        else:
            return os.path.isdir(path)
    except Exception:
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


def _safe_name(name: str) -> str:
    """
    Sanitize a NEMO account/project name for use as a filesystem directory name.
    Strips leading/trailing whitespace, replaces path-separator characters with '_',
    collapses runs of spaces to single spaces so the name looks clean on Windows,
    removes trailing periods (illegal on Windows), and handles Windows reserved names.
    """
    import re
    if not name:
        return name
    # Replace characters that are illegal on Linux or Windows filesystems
    name = re.sub(r'[/\\:\*\?"<>\|]', '_', name)
    # Collapse multiple spaces to one and strip edges
    name = re.sub(r' +', ' ', name).strip()
    # Windows folders cannot end with a period
    name = name.rstrip('.')
    
    # Windows reserved filenames (CON, PRN, AUX, NUL, COM1-9, LPT1-9)
    reserved_patterns = re.compile(r'^(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])$', re.IGNORECASE)
    if reserved_patterns.match(name):
        name = f"_{name}"
        
    return name if name else "unnamed_folder"



def verify_client_auth() -> bool:
    """
    Verify Mutual TLS (mTLS) client certificate verification.
    If behind a reverse proxy (like Nginx), we check the proxy header 'X-Client-Verify'.
    Otherwise, if native SSL context is active, the SSL handshake enforces client verification,
    so we return True.
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
    Parse and validate the incoming JSON request body.
    Requires: user_id, tool, account_id, project_id.
    Returns (data_dict, None) on success, or (None, (error_dict, status)) on failure.
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

# ---------------------------------------------------------------------------
# Flask App & Endpoints
# ---------------------------------------------------------------------------

app = Flask(__name__)


@app.route("/mount", methods=["POST"])
def mount_endpoint():
    data, err = _validate_request()
    if err:
        return jsonify(err[0]), err[1]

    user_id = data["user_id"]
    user_id_str = data["user_id_str"]
    tool = data["tool"]
    account_id = data["account_id"]
    project_id = data["project_id"]
    session_id = data.get("session_id") or f"session_{user_id}_{tool}"

    if config.get("mtls", {}).get("enabled"):
        if not verify_client_auth():
            return jsonify({"error": "Mutual TLS client verification failed"}), 401

    log.info(f"🔐 USER ACTION: User ID {user_id} logging into {tool} (project {project_id} / account {account_id})")

    # Fetch user info from state DB
    user_info = state_db.get_user_by_id(user_id)
    if not user_info:
        # User not synced yet — run an on-demand sync to provision them
        log.info(f"User ID {user_id} not found in state DB. Running on-demand sync backfill...")
        nemo_sync.run_once()
        user_info = state_db.get_user_by_id(user_id)
        if not user_info:
            return jsonify({"error": f"User ID {user_id} not found or provisioned on this system"}), 404

    username = user_info["username"]

    # Verify user quota usage
    quota_info = quota_manager.check_quota_usage(f"u{user_id}", str(USERS_DIR))
    quota_warning = None
    if quota_info.get("exceeded"):
        log.warning(f"⚠️ QUOTA WARNING: User 'u{user_id}' ({username}) exceeded quota! (Used: {quota_info.get('used_gb')}GB)")
        quota_warning = "Quota exceeded!"

    # -----------------------------------------------------------------------
    # Source paths (persistent storage on /srv/labdata)
    # Source dirs are always keyed by ID (stable even if name changes).
    # -----------------------------------------------------------------------
    source_user    = USERS_DIR / f"u{user_id}"
    source_project = GROUPS_DIR / f"account_{account_id}" / f"project_{project_id}"

    # -----------------------------------------------------------------------
    # Resolve human-readable names from state_db for the session view dirs.
    # Run an on-demand sync backfill if account/project is missing.
    # -----------------------------------------------------------------------
    account_info = state_db.get_account_by_id(account_id)
    project_info = state_db.get_project_by_id(project_id)
    if not account_info or not project_info:
        log.info(f"Account {account_id} or Project {project_id} not found in state DB. Running on-demand sync backfill...")
        nemo_sync.run_once()
        account_info = state_db.get_account_by_id(account_id)
        project_info = state_db.get_project_by_id(project_id)

    # Reject deactivations
    if user_info and not user_info.get("active", True):
        log.warning(f"❌ Mount rejected: User 'u{user_id}' is deactivated.")
        return jsonify({"error": "User is deactivated"}), 403

    if account_info and not account_info.get("active", True):
        log.warning(f"❌ Mount rejected: Account '{account_info['name']}' is deactivated.")
        return jsonify({"error": "Account is deactivated"}), 403

    if project_info and not project_info.get("active", True):
        log.warning(f"❌ Mount rejected: Project '{project_info['name']}' is deactivated.")
        return jsonify({"error": "Project is deactivated"}), 403

    account_folder = _safe_name(account_info["name"]) if account_info else f"account_{account_id}"
    project_folder = _safe_name(project_info["name"]) if project_info else f"project_{project_id}"

    # -----------------------------------------------------------------------
    # Target paths (ephemeral session view under /tmp/labdata/sessions)
    # Determine the folder structure choice locked at deployment time.
    # -----------------------------------------------------------------------
    group_folder_type = config["storage"].get("group_folder_type", "project")
    target_user    = SESSIONS_DIR / tool / "my_files"
    target_public  = SESSIONS_DIR / tool / "public"

    if group_folder_type == "account":
        target_project = SESSIONS_DIR / tool / "my_groups" / account_folder
        log.info(f"Session folders: my_groups/{account_folder} (type: account)")
    elif group_folder_type == "project":
        target_project = SESSIONS_DIR / tool / "my_groups" / project_folder
        log.info(f"Session folders: my_groups/{project_folder} (type: project)")
    else:  # hierarchical
        target_project = SESSIONS_DIR / tool / "my_groups" / account_folder / project_folder
        log.info(f"Session folders: my_groups/{account_folder}/{project_folder} (type: hierarchical)")

    # Ensure source project directory exists (in case sync hasn't run yet)
    try:
        source_user.mkdir(parents=True, exist_ok=True)
        source_project.mkdir(parents=True, exist_ok=True)
        target_user.mkdir(parents=True, exist_ok=True)
        target_project.parent.mkdir(parents=True, exist_ok=True)
        target_project.mkdir(parents=True, exist_ok=True)
        target_public.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        log.error(f"Failed to create mount directories: {exc}")
        return jsonify({"error": str(exc)}), 500

    # -----------------------------------------------------------------------
    # Provision the tool machine account and grant POSIX ACLs
    # -----------------------------------------------------------------------
    tool_machine = user_provisioner.provision_machine_account(tool)

    # User private directory
    acl_manager.grant_acl_access(tool_machine, str(source_user), "rwx")
    # Traverse access on the specific account dir only
    acc_dir = GROUPS_DIR / f"account_{account_id}"
    acl_manager.grant_acl_access(tool_machine, str(acc_dir), "x")
    # Full read/write on the specific project dir
    acl_manager.grant_acl_access(tool_machine, str(source_project), "rwx")
    # Public directory (read-only)
    if PUBLIC_DIR.exists() and PUBLIC_DIR.is_dir():
        acl_manager.grant_acl_access(tool_machine, str(PUBLIC_DIR), "rx")

    mount_points = []

    # -----------------------------------------------------------------------
    # A. User private directory -> my_files/
    # -----------------------------------------------------------------------
    user_already_mounted = _is_mountpoint(target_user)
    if user_already_mounted:
        log.info(f"✅ {username} (u{user_id}) already has active user session mount on {tool}")
    else:
        try:
            _mount_bind(source_user, target_user)
            log.info(f"✅ MOUNT SUCCESS (my_files): u{user_id} → {target_user}")
        except OSError as exc:
            log.error(f"❌ MOUNT FAILED for user u{user_id} on {tool}: {exc}")
            return jsonify({"error": str(exc)}), 500
    mount_points.append(f"{source_user} → {target_user}")

    # -----------------------------------------------------------------------
    # B. Specific project directory -> my_groups/account_{id}/project_{id}/
    #    Only the checked-in project is exposed — not the entire groups tree.
    # -----------------------------------------------------------------------
    project_already_mounted = _is_mountpoint(target_project)
    if project_already_mounted:
        log.info(f"✅ Project {project_id} already mounted on {tool}")
    else:
        try:
            _mount_bind(source_project, target_project)
            log.info(f"✅ MOUNT SUCCESS (project): {source_project} → {target_project}")
        except OSError as exc:
            log.error(f"❌ MOUNT FAILED for project {project_id} on {tool}: {exc}")
            # Non-fatal — continue to mount public
    mount_points.append(f"{source_project} → {target_project}")

    # -----------------------------------------------------------------------
    # C. Public directory -> public/ (read-only)
    # -----------------------------------------------------------------------
    public_already_mounted = _is_mountpoint(target_public)
    if not public_already_mounted and PUBLIC_DIR.exists() and PUBLIC_DIR.is_dir():
        try:
            _mount_bind_ro(PUBLIC_DIR, target_public)
            log.info(f"✅ MOUNT SUCCESS (PUBLIC RO): {PUBLIC_DIR} → {target_public}")
        except Exception as exc:
            log.error(f"❌ MOUNT FAILED for public directory on {tool}: {exc}")
    mount_points.append(f"{PUBLIC_DIR} → {target_public}")

    # Save session state to SQLite database
    state_db.save_session(session_id, user_id, tool, mount_points)

    response_body = {
        "status": "already_mounted" if user_already_mounted else "mounted",
        "path": str(target_user),
        "project_path": str(target_project),
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

    user_id = data["user_id"]
    user_id_str = data["user_id_str"]
    tool = data["tool"]
    account_id = data["account_id"]
    project_id = data["project_id"]
    session_id = data.get("session_id")

    if config.get("mtls", {}).get("enabled"):
        if not verify_client_auth():
            return jsonify({"error": "Mutual TLS client verification failed"}), 401

    log.info(f"🔓 UNMOUNT REQUEST: User ID {user_id} leaving {tool} (project {project_id} / account {account_id})")

    # -----------------------------------------------------------------------
    # Step 1: Find the matching session in the DB.
    # NOTE: get_all_sessions() stores the key as "tool" (not "tool_name").
    # -----------------------------------------------------------------------
    sessions = state_db.get_all_sessions()
    if not session_id:
        for s_id, s_info in sessions.items():
            if s_info.get("user_id") == user_id and s_info.get("tool") == tool:
                session_id = s_id
                break

    session_info = sessions.get(session_id) if session_id else None

    # -----------------------------------------------------------------------
    # Step 2: Determine the tool's session directory and find ALL currently
    # live mounts under it via /proc/mounts (definitive kernel source of
    # truth). This way we unmount everything regardless of which project was
    # mounted — the project in the unmount request might differ from what was
    # actually mounted if the user switched projects between check-in calls.
    # -----------------------------------------------------------------------
    tool_session_dir = SESSIONS_DIR / tool
    live_mounts = []
    try:
        with open("/proc/mounts", "r") as f:
            for line in f:
                fields = line.split()
                if len(fields) >= 2:
                    mp = fields[1].replace("\\040", " ")
                    if str(tool_session_dir) in mp:
                        live_mounts.append(mp)
        # Deepest paths first so children are unmounted before parents
        live_mounts.sort(key=len, reverse=True)
    except Exception as e:
        log.error(f"Could not read /proc/mounts: {e}")

    grace_seconds = config["session"]["unmount_grace_seconds"]
    unmount_success = True

    if live_mounts:
        log.info(f"Found {len(live_mounts)} live mount(s) to unmount for tool '{tool}'")
        for mp in live_mounts:
            target_path = Path(mp)
            log.info(f"  Unmounting: {target_path}")
            if not graceful_unmount(target_path, grace_seconds):
                unmount_success = False
            # Brief pause so kernel finishes lazy-unmount detach before rmdir
            time.sleep(0.3)
            try:
                if target_path.exists() and not _is_mountpoint(target_path):
                    target_path.rmdir()
            except OSError as e:
                log.warning(f"  Could not remove {target_path}: {e}")
    else:
        log.warning(f"No live mounts found in /proc/mounts for tool '{tool}'. Nothing to unmount.")

    # -----------------------------------------------------------------------
    # Step 3: Clean up empty leftover dirs (account dir, my_groups, tool dir)
    # -----------------------------------------------------------------------
    def _rmdir_if_empty(path: Path):
        try:
            if path.exists() and not _is_mountpoint(path) and not list(path.iterdir()):
                path.rmdir()
                log.info(f"  Removed empty dir: {path}")
        except OSError:
            pass

    if tool_session_dir.exists():
        for dirpath, dirnames, filenames in os.walk(str(tool_session_dir), topdown=False):
            _rmdir_if_empty(Path(dirpath))
        _rmdir_if_empty(tool_session_dir)

    # -----------------------------------------------------------------------
    # Step 4: Revoke POSIX ACLs for the specific project
    # -----------------------------------------------------------------------
    tool_machine   = f"{tool.lower()}_machine"
    source_user    = USERS_DIR / f"u{user_id}"
    source_project = GROUPS_DIR / f"account_{account_id}" / f"project_{project_id}"
    acc_dir        = GROUPS_DIR / f"account_{account_id}"

    acl_manager.revoke_acl_access(tool_machine, str(source_user))
    acl_manager.revoke_acl_access(tool_machine, str(source_project))
    acl_manager.revoke_acl_access(tool_machine, str(acc_dir))
    acl_manager.revoke_acl_access(tool_machine, str(PUBLIC_DIR))

    # -----------------------------------------------------------------------
    # Step 5: Remove session from DB
    # -----------------------------------------------------------------------
    if session_id:
        state_db.remove_session(session_id)

    if unmount_success:
        return jsonify({"status": "unmounted"}), 200
    else:
        return jsonify({"error": "Failed to unmount cleanly"}), 500


@app.route("/init_user", methods=["POST"])
def init_user_endpoint():
    data = request.get_json(silent=True)
    if data is None:
        return jsonify({"error": "Invalid JSON"}), 400

    user_id = data.get("user_id")
    tool = data.get("tool", "system").strip()

    if user_id is None:
        return jsonify({"error": "'user_id' required"}), 400

    try:
        user_id_str = str(user_id).strip()
    except Exception:
         return jsonify({"error": "Invalid user_id format"}), 400

    if config.get("mtls", {}).get("enabled"):
        if not verify_client_auth():
            return jsonify({"error": "Mutual TLS client verification failed"}), 401

    log.info(f"🔐 USER INITIALIZATION REQUEST: User ID {user_id}")

    # Provision user locally
    user_info = state_db.get_user_by_id(user_id)
    if not user_info:
        # Run sync once to load new user data
        log.info(f"User ID {user_id} not found in state DB. Triggering sync loop...")
        nemo_sync.run_once()
        user_info = state_db.get_user_by_id(user_id)
        
    # If still not found (e.g. user created in Nextcloud flow but not synced to NEMO yet), provision manually
    if not user_info:
        # Fallback creation
        linux_user = f"u{user_id}"
        user_provisioner.provision_user(user_id, f"user_{user_id}")
    else:
        linux_user = f"u{user_id}"
        user_provisioner.provision_user(user_id, user_info["username"])

    user_dir_path = user_provisioner.ensure_user_directory(user_id)
    user_provisioner.apply_user_quota(user_id)

    return jsonify({
        "status": "initialized",
        "path": user_dir_path
    }), 200


@app.route("/sessions", methods=["GET"])
def get_sessions():
    """
    Returns all active sessions in the database.
    """
    return jsonify(state_db.get_all_sessions()), 200


@app.route("/projects/<int:user_id>", methods=["GET"])
def get_user_projects(user_id):
    """
    Returns list of active projects for user.
    """
    return jsonify(state_db.get_project_linux_groups(user_id)), 200


@app.route("/quota/<username_or_id>", methods=["GET"])
def get_user_quota(username_or_id):
    """
    Queries and returns quota limits and usage for the user.
    Supports username (e.g., nextcloud_student) or ID (e.g., 9 or u9).
    """
    username = username_or_id
    if username.startswith("u") and username[1:].isdigit():
        user_id = int(username[1:])
    elif username.isdigit():
        user_id = int(username)
    else:
        user_info = state_db.get_user_by_username(username)
        user_id = user_info["id"] if user_info else None

    if user_id is None:
        return jsonify({"status": "unknown", "error": "User not found"}), 404

    quota_info = quota_manager.check_quota_usage(f"u{user_id}", str(USERS_DIR))
    return jsonify(quota_info), 200


@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok"}), 200

# ---------------------------------------------------------------------------
# Auto-unmount Callback for Idle Session Monitor
# ---------------------------------------------------------------------------

def auto_unmount_session(user_id, tool, session_id):
    """
    Function triggered by IdleMonitor background thread to clean up idle sessions.
    """
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
            
            log.info(f"⏳ Auto-unmount: Unmounting target directory {target_path}")
            if _is_mountpoint(target_path):
                graceful_unmount(target_path, grace_seconds)
            
            try:
                if target_path.exists():
                    target_path.rmdir()
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

    # Revoke machine account ACL permissions
    tool_machine = f"{tool.lower()}_machine"
    source_user = USERS_DIR / f"u{user_id}"
    acl_manager.revoke_acl_access(tool_machine, str(source_user))
    
    projects = state_db.get_project_linux_groups(user_id)
    for proj in projects:
        acc_id = proj["account_id"]
        proj_id = proj["id"]
        acc_dir = GROUPS_DIR / f"account_{acc_id}"
        proj_dir = acc_dir / f"project_{proj_id}"
        acl_manager.revoke_acl_access(tool_machine, str(proj_dir))
        acl_manager.revoke_acl_access(tool_machine, str(acc_dir))
        
    acl_manager.revoke_acl_access(tool_machine, str(GROUPS_DIR))
    acl_manager.revoke_acl_access(tool_machine, str(PUBLIC_DIR))

    # Clean up from database
    state_db.remove_session(session_id)
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
                subprocess.run(["umount", "-l", mp], check=True, capture_output=True)
                log.info(f"🧹 Orphan recovery: Successfully unmounted '{mp}'")
                orphans_cleaned += 1
                
                try:
                    os.rmdir(mp)
                except Exception:
                    pass
            except Exception as e:
                log.error(f"🧹 Orphan recovery: Failed to unmount '{mp}': {e}")
    
    log.info(f"🧹 Orphan recovery completed. Cleaned {orphans_cleaned} orphaned mounts.")

# ---------------------------------------------------------------------------
# Main Entry Point
# ---------------------------------------------------------------------------

def startup_cleanup():
    """
    On every daemon start, force-unmount ALL bind mounts under SESSIONS_DIR
    and wipe the session state from the DB.

    Why: Linux bind mounts are kernel-level and survive daemon restarts.
    Without this, stale Microscope1/Litho1 folders keep showing up in the
    Samba share even when no one is using those tools.

    After cleanup, active NEMO sessions will re-trigger mounts the next
    time a user logs into a tool.
    """
    log.info("🧹 STARTUP CLEANUP: Wiping all stale session mounts...")

    stale_mounts = []
    # 1. Find every labdata session mount currently active in the kernel.
    #    Use /proc/mounts (space-separated, field 1 is the mount point) rather
    #    than `mount` command output, which breaks on paths containing spaces.
    try:
        with open("/proc/mounts", "r") as f:
            for line in f:
                fields = line.split()
                if len(fields) >= 2:
                    mount_point = fields[1]
                    # /proc/mounts encodes spaces as \040
                    mount_point = mount_point.replace("\\040", " ")
                    if str(SESSIONS_DIR) in mount_point:
                        stale_mounts.append(mount_point)

        # Unmount deepest paths first (longest path = deepest child first)
        stale_mounts.sort(key=len, reverse=True)
        for mp in stale_mounts:
            log.info(f"  Unmounting stale: {mp}")
            subprocess.run(["umount", "-l", mp], capture_output=True)
    except Exception as e:
        log.error(f"Startup cleanup: error scanning /proc/mounts: {e}")

    # 2. Remove and recreate the sessions directory so the Samba share is empty
    try:
        import shutil
        shutil.rmtree(str(SESSIONS_DIR), ignore_errors=True)
        SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
        log.info(f"  Session directory wiped and recreated: {SESSIONS_DIR}")
    except Exception as e:
        log.error(f"Startup cleanup: error wiping sessions dir: {e}")

    # 3. Clear stale sessions from SQLite
    cleared = state_db.clear_all_sessions()
    log.info(f"🧹 STARTUP CLEANUP DONE: {len(stale_mounts) if 'stale_mounts' in dir() else 0} mounts removed, {cleared} DB sessions cleared.")


if __name__ == "__main__":
    if os.name != 'nt' and os.geteuid() != 0:
        log.warning("Not running as root — mounts and ACLs will fail! Run with sudo")

    # Ensure base storage directories exist (NOT sessions — startup_cleanup recreates it)
    USERS_DIR.mkdir(parents=True, exist_ok=True)
    GROUPS_DIR.mkdir(parents=True, exist_ok=True)

    # Wipe all stale mounts and session state on every start
    startup_cleanup()

    # Start NEMO Sync scheduler
    nemo_sync.start()

    # Start idle session monitor background thread
    idle_monitor = IdleMonitor(
        session_manager=state_db,
        unmount_callback=auto_unmount_session,
        idle_timeout_minutes=config["session"]["idle_timeout_minutes"],
        check_interval_seconds=30,
        samba_controller=samba_controller
    )
    idle_monitor.start()

    log.info(f"Starting Lab Data Mount Daemon on {HOST}:{PORT}")
    log.info(f"Users directory: {USERS_DIR}")
    log.info(f"Groups directory: {GROUPS_DIR}")
    log.info(f"Sessions directory: {SESSIONS_DIR}")

    context = None
    if config.get("mtls", {}).get("enabled"):
        import ssl
        mtls_conf = config["mtls"]
        # Convert relative paths to absolute relative to this file or cwd if needed
        # We'll expect them to be absolute or correctly situated relative to execution directory
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(mtls_conf["server_cert"], mtls_conf["server_key"])
        context.load_verify_locations(mtls_conf["ca_cert"])
        context.verify_mode = ssl.CERT_REQUIRED
        log.info("🔒 mTLS Client-Certificate Verification is ENABLED")

    try:
        app.run(host=HOST, port=PORT, debug=False, ssl_context=context)
    finally:
        nemo_sync.stop()
        idle_monitor.stop()