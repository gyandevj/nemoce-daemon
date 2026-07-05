#!/usr/bin/env python3
"""
Lab Data Mount Daemon Controller (Privileged Worker)
===================================================
Runs as root. Listens on a local Unix socket.
Handles low-level operations (mount, umount, setfacl, setquota).
Also hosts background threads for NemoSync and IdleMonitor.
"""

import os
import sys
import time
import json
import socket
import grp
import pwd
import ctypes
import ctypes.util
import logging
import subprocess
import threading
import signal
from pathlib import Path

import yaml

# Import custom modules
from modules.state_db import StateDB
from modules.nemo_api_client import NemoAPIClient
from modules.user_provisioner import UserProvisioner
from modules.samba_controller import SambaController
from modules.nemo_sync import NemoSync
from modules.socket_comm import send_msg, recv_msg
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
    handler.setFormatter(logging.Formatter('%(asctime)s [%(levelname)s] (Controller) %(message)s'))
    log.addHandler(handler)

# ---------------------------------------------------------------------------
# Configuration Loading
# ---------------------------------------------------------------------------
DEFAULT_CONFIG = {
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
        "socket_path": "/var/run/lab-daemon/lab-daemon.sock",
        "idle_timeout_minutes": 60,
        "unmount_grace_seconds": 30
    },
    "nemo": {
        "django_path": "/mnt/c/Users/gyand/Desktop/NemoProject/nemo-ce",
        "poll_interval_seconds": 3600,
        "use_api_http": False,
        "api_url": "http://localhost:8000",
        "api_token": "nemo_api_token_here_replace_me"
    },
    "sync": {
        "on_deactivation": "lock_account",
        "dry_run": False
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
                # Merge nested dicts
                for key in ("storage", "quota", "session", "nemo", "sync"):
                    if key in user_config and isinstance(user_config[key], dict):
                        config[key].update(user_config[key])
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
    if not _libc:
        log.warning(f"Mocking Bind Mount: {source} → {target}")
        return
    ret = _libc.mount(str(source).encode(), str(target).encode(), None, MS_BIND, None)
    if ret != 0:
        err = ctypes.get_errno()
        raise OSError(err, os.strerror(err))

def _mount_bind_ro(source: Path, target: Path) -> None:
    target.mkdir(parents=True, exist_ok=True)
    if not _libc:
        log.warning(f"Mocking RO Bind Mount: {source} → {target}")
        return
    ret = _libc.mount(str(source).encode(), str(target).encode(), None, MS_BIND, None)
    if ret != 0:
        err = ctypes.get_errno()
        raise OSError(err, os.strerror(err))
    ret = _libc.mount(None, str(target).encode(), None, MS_BIND | MS_REMOUNT | MS_RDONLY, None)
    if ret != 0:
        err = ctypes.get_errno()
        raise OSError(err, os.strerror(err))

def _umount(target: Path) -> None:
    if not _libc:
        log.warning(f"Mocking Unmount: {target}")
        return
    ret = _libc.umount(str(target).encode())
    if ret != 0:
        err = ctypes.get_errno()
        raise OSError(err, os.strerror(err))

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
    source_project = GROUPS_DIR / f"account_{account_id}" / f"project_{project_id}"

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

    project_excluded = is_project_excluded(project_id, project_name, account_id, account_name)

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

    # Grant ACL on the user's personal files directory (not the full home)
    source_user.mkdir(parents=True, exist_ok=True)
    source_files.mkdir(parents=True, exist_ok=True)
    acl_manager.grant_acl_access(tool_machine, str(source_files), "rwx")

    mount_points = []
    quota_warning = None

    # Quota check
    quota_info = quota_manager.check_quota_usage(f"u{user_id}", str(USERS_DIR))
    if quota_info.get("exceeded"):
        quota_warning = "Quota exceeded!"

    # User (my_files) Mount — bind only the personal files subdir, not the full home
    user_already_mounted = _is_mountpoint(target_user)
    if not user_already_mounted:
        try:
            _mount_bind(source_files, target_user)
        except OSError as exc:
            return {"error": f"User mount failed: {exc}"}, 500
    mount_points.append(f"{source_files} → {target_user}")

    # Create empty account navigation folders under my_groups (no bind mounts).
    # Deduplication: track which account_ids have already been created.
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

    persistent_dirs = {
        tool_session_dir,
        tool_session_dir / "my_files",
        tool_session_dir / "my_groups",
        tool_session_dir / "public"
    }

    if live_mounts:
        for mp in live_mounts:
            target_path = Path(mp)
            if not graceful_unmount(target_path, grace_seconds):
                unmount_success = False
            time.sleep(0.3)
            try:
                # Do NOT remove standard persistent directories
                abs_path = target_path.resolve()
                if abs_path in [p.resolve() for p in persistent_dirs]:
                    continue
                if abs_path.exists() and not _is_mountpoint(abs_path):
                    abs_path.rmdir()
            except OSError as e:
                log.warning(f"Could not remove {target_path}: {e}")

    # Remove empty dirs inside my_groups (leaving parent and standard folders intact)
    my_groups_dir = tool_session_dir / "my_groups"
    if my_groups_dir.exists():
        import shutil
        for item in my_groups_dir.iterdir():
            if item.is_dir() and not _is_mountpoint(item):
                try:
                    shutil.rmtree(item)
                except Exception as e:
                    log.warning(f"Could not remove dynamic user project dir {item}: {e}")

    # Revoke ACLs for user dir, public, and ALL accounts the user belongs to
    tool_machine = f"{tool_lower}_machine"
    source_user = USERS_DIR / f"u{user_id}"
    acl_manager.revoke_acl_access(tool_machine, str(source_user))
    acl_manager.revoke_acl_access(tool_machine, str(PUBLIC_DIR))

    # Revoke for every account/project the user is a member of
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
    # Look up session's project/account from database before removing
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
# Unix Socket Server Loop
# ---------------------------------------------------------------------------
class SocketServer:
    def __init__(self, socket_path):
        self.socket_path = socket_path
        self.server_sock = None
        self._running = False
        self._thread = None

    def start(self):
        self._running = True
        self._thread = threading.Thread(target=self._run_loop, name="socket-server", daemon=True)
        self._thread.start()

    def stop(self):
        self._running = False
        if self.server_sock:
            try:
                self.server_sock.close()
            except Exception:
                pass
        if self._thread:
            self._thread.join(timeout=2.0)
        # Cleanup socket file
        if os.path.exists(self.socket_path):
            try:
                os.unlink(self.socket_path)
            except Exception:
                pass

    def _run_loop(self):
        # Ensure parent directory exists
        sock_dir = os.path.dirname(self.socket_path)
        if sock_dir:
            os.makedirs(sock_dir, exist_ok=True)
            # Try to chown directory to root:www-data
            try:
                # Resolve www-data group gid
                try:
                    gid = grp.getgrnam("www-data").gr_gid
                except KeyError:
                    gid = os.getgid()
                os.chown(sock_dir, 0, gid)
                os.chmod(sock_dir, 0o750)
            except Exception as e:
                log.debug(f"Could not set permissions on socket directory: {e}")

        # Cleanup old socket file if exists
        if os.path.exists(self.socket_path):
            try:
                os.unlink(self.socket_path)
            except OSError:
                pass

        self.server_sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            self.server_sock.bind(self.socket_path)
            self.server_sock.listen(5)
        except Exception as e:
            log.error(f"Failed to bind Unix socket at {self.socket_path}: {e}")
            self._running = False
            return

        # Set permissions on socket file
        try:
            try:
                gid = grp.getgrnam("www-data").gr_gid
            except KeyError:
                gid = os.getgid()
            os.chown(self.socket_path, 0, gid)
            os.chmod(self.socket_path, 0o660)
        except Exception as e:
            log.debug(f"Could not set permissions on socket file: {e}")

        log.info(f"Controller listening on Unix Socket: {self.socket_path}")
        self.server_sock.settimeout(1.0)

        while self._running:
            try:
                conn, addr = self.server_sock.accept()
            except socket.timeout:
                continue
            except Exception:
                break

            # Handle connection in a new thread
            t = threading.Thread(target=self._handle_connection, args=(conn,), daemon=True)
            t.start()

    def _handle_connection(self, conn):
        try:
            while self._running:
                cmd = recv_msg(conn)
                if cmd is None:
                    break # Connection closed
                
                action = cmd.get("action")
                payload = cmd.get("payload", {})
                
                log.debug(f"Received command: {action} with payload {payload}")
                
                # Route action
                try:
                    if action == "mount":
                        res, code = handle_mount(payload)
                        response = {"status": "success" if code in (200, 201) else "error", "code": code, "result": res}
                    elif action == "unmount":
                        res, code = handle_unmount(payload)
                        response = {"status": "success" if code == 200 else "error", "code": code, "result": res}
                    elif action == "init_user":
                        res, code = handle_init_user(payload)
                        response = {"status": "success" if code == 200 else "error", "code": code, "result": res}
                    elif action == "sessions":
                        res, code = handle_sessions(payload)
                        response = {"status": "success", "code": code, "result": res}
                    elif action == "projects":
                        res, code = handle_projects(payload)
                        response = {"status": "success" if code == 200 else "error", "code": code, "result": res}
                    elif action == "quota":
                        res, code = handle_quota(payload)
                        response = {"status": "success" if code == 200 else "error", "code": code, "result": res}
                    elif action == "health":
                        response = {"status": "success", "code": 200, "result": {"status": "ok"}}
                    else:
                        response = {"status": "error", "code": 400, "error": f"Unknown action: {action}"}
                except Exception as e:
                    log.error(f"Error executing action '{action}': {e}", exc_info=True)
                    response = {"status": "error", "code": 500, "error": str(e)}
                
                send_msg(conn, response)
        except Exception as e:
            log.error(f"Error in connection handling: {e}")
        finally:
            conn.close()

# ---------------------------------------------------------------------------
# Signal Handling & Main Execution
# ---------------------------------------------------------------------------
socket_server = None

def sigterm_handler(signum, frame):
    log.info("Terminating Controller...")
    if nemo_sync:
        nemo_sync.stop()
    if idle_monitor:
        idle_monitor.stop()
    if socket_server:
        socket_server.stop()
    sys.exit(0)

if __name__ == "__main__":
    # Ensure running as root
    if os.getuid() != 0 and sys.platform != "win32":
        log.error("Daemon Controller must run as root!")
        sys.exit(1)

    signal.signal(signal.SIGTERM, sigterm_handler)
    signal.signal(signal.SIGINT, sigterm_handler)

    # Start background loops
    nemo_sync.start()
    idle_monitor.start()

    # Start Unix Socket Server
    socket_server = SocketServer(config["session"]["socket_path"])
    socket_server.start()

    # Keep main thread alive
    log.info("Daemon Controller started successfully. Press Ctrl+C to exit.")
    while True:
        try:
            time.sleep(1.0)
        except KeyboardInterrupt:
            break

    # Clean cleanup
    sigterm_handler(None, None)
