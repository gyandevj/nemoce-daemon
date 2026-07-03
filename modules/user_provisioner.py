import os
import subprocess
import logging
from pathlib import Path

logger = logging.getLogger("lab-daemon")

# Try to import pwd/grp for user/group checks on Unix, fall back to None on Windows
try:
    import pwd
    import grp
except ImportError:
    pwd = None
    grp = None

class UserProvisioner:
    """
    Handles OS-level user, group, directory, ACL, and quota provisioning.
    """
    def __init__(self, base_path: str, users_path: str, groups_path: str, quota_soft_gb=10, quota_hard_gb=12):
        self.base_path = Path(base_path)
        self.users_path = Path(users_path)
        self.groups_path = Path(groups_path)
        self.quota_soft_gb = quota_soft_gb
        self.quota_hard_gb = quota_hard_gb

    def _run_cmd(self, cmd: list) -> bool:
        try:
            logger.debug(f"Running command: {' '.join(cmd)}")
            res = subprocess.run(cmd, capture_output=True, text=True, check=True)
            return True
        except FileNotFoundError:
            logger.warning(f"Executable not found for command: {cmd[0]}. Mocking/skipping.")
            return True
        except subprocess.CalledProcessError as e:
            logger.error(f"Command failed: {' '.join(cmd)}. Error: {e.stderr.strip()}")
            return False
        except Exception as e:
            logger.error(f"Unexpected error running command {' '.join(cmd)}: {e}")
            return False

    def group_exists(self, group_name: str) -> bool:
        if grp:
            try:
                grp.getgrnam(group_name)
                return True
            except KeyError:
                return False
        # Fallback check for Windows / non-grp systems
        try:
            res = subprocess.run(["getent", "group", group_name], capture_output=True)
            return res.returncode == 0
        except Exception:
            return False

    def user_exists(self, username: str) -> bool:
        if pwd:
            try:
                pwd.getpwnam(username)
                return True
            except KeyError:
                return False
        # Fallback check for Windows / non-pwd systems
        try:
            res = subprocess.run(["id", username], capture_output=True)
            return res.returncode == 0
        except Exception:
            return False

    def provision_group(self, project_id: int) -> str:
        group_name = f"proj_{project_id}"
        if not self.group_exists(group_name):
            logger.info(f"Group '{group_name}' does not exist. Creating it...")
            self._run_cmd(["groupadd", group_name])
        return group_name

    def provision_user(self, user_id: int, username: str) -> str:
        linux_user = f"u{user_id}"
        if not self.user_exists(linux_user):
            logger.info(f"Linux user '{linux_user}' (NEMO username: '{username}') does not exist. Creating it...")
            # Create system user with no home directory creation, shell=nologin, primary group nogroup
            self._run_cmd(["useradd", "-M", "-g", "nogroup", "-s", "/usr/sbin/nologin", "-c", f"NEMO User {username}", linux_user])
        return linux_user

    def provision_machine_account(self, tool_name: str) -> str:
        machine_user = f"{tool_name.lower()}_machine"
        if not self.user_exists(machine_user):
            logger.info(f"Machine user '{machine_user}' does not exist. Creating it...")
            # Create system user with no home directory creation, shell=nologin, primary group nogroup
            self._run_cmd(["useradd", "-M", "-g", "nogroup", "-s", "/usr/sbin/nologin", "-c", f"NEMO Machine {tool_name}", machine_user])
        return machine_user

    def set_user_active_status(self, user_id: int, active: bool, policy: str):
        linux_user = f"u{user_id}"
        if not self.user_exists(linux_user):
            return
            
        if not active:
            if policy == "lock_account":
                logger.info(f"Locking Linux account: {linux_user}")
                self._run_cmd(["usermod", "-L", linux_user])
            elif policy == "remove_membership_only":
                logger.info(f"Stripping group memberships for locked user: {linux_user}")
                self._run_cmd(["usermod", "-G", "", linux_user])
            elif policy == "ignore":
                pass
        else:
            # Unlock account if previously locked
            logger.info(f"Unlocking Linux account (if locked): {linux_user}")
            # usermod -U can return code 1 if account wasn't locked (no password), so we ignore failure here
            try:
                subprocess.run(["usermod", "-U", linux_user], capture_output=True)
            except Exception:
                pass

    def sync_user_groups(self, user_id: int, project_ids: list):
        linux_user = f"u{user_id}"
        if not self.user_exists(linux_user):
            return
            
        groups = [f"proj_{pid}" for pid in project_ids]
        # Always verify all project groups exist first
        for pid in project_ids:
            self.provision_group(pid)
            
        # Set exact group membership
        groups_str = ",".join(groups)
        logger.info(f"Syncing group memberships for '{linux_user}': {groups}")
        self._run_cmd(["usermod", "-G", groups_str, linux_user])

    def ensure_user_directory(self, user_id: int) -> str:
        linux_user = f"u{user_id}"
        user_dir = self.users_path / linux_user
        
        if not user_dir.exists():
            user_dir.mkdir(parents=True, exist_ok=True)
            logger.info(f"Created user private directory: {user_dir}")
            
        # Set ownership: u{id}:nogroup
        # On Windows, grp/pwd might be mock so we shell out chown
        try:
            subprocess.run(["chown", "-R", f"{linux_user}:nogroup", str(user_dir)], capture_output=True)
            subprocess.run(["chmod", "770", str(user_dir)], capture_output=True)
            # Grant NextCloud daemon (www-data) access to read/write this user folder via ACLs
            subprocess.run(["setfacl", "-m", "u:www-data:rwx", str(user_dir)], capture_output=True)
            subprocess.run(["setfacl", "-d", "-m", "u:www-data:rwx", str(user_dir)], capture_output=True)
            # Ensure any files/folders created by Nextcloud give the user full access
            subprocess.run(["setfacl", "-m", f"u:{linux_user}:rwx", str(user_dir)], capture_output=True)
            subprocess.run(["setfacl", "-d", "-m", f"u:{linux_user}:rwx", str(user_dir)], capture_output=True)
        except Exception as e:
            logger.debug(f"Failed to set directory permissions/chown on '{user_dir}': {e}")
            
        return str(user_dir)

    def ensure_project_directory(self, account_id: int, project_id: int) -> str:
        proj_group = f"proj_{project_id}"
        account_dir = self.groups_path / f"account_{account_id}"
        project_dir = account_dir / f"project_{project_id}"
        
        # Ensure parent account folder exists
        if not account_dir.exists():
            account_dir.mkdir(parents=True, exist_ok=True)
            # Give rwxrwxr-x permissions to account directories so users can traverse them
            try:
                subprocess.run(["chmod", "775", str(account_dir)], capture_output=True)
            except Exception:
                pass
                
        # Ensure project directory exists
        if not project_dir.exists():
            project_dir.mkdir(parents=True, exist_ok=True)
            logger.info(f"Created project directory: {project_dir}")
            
        # Set ownership: root:proj_{project_id}
        # Set permissions: 770 (owner/group full access, others none)
        try:
            subprocess.run(["chown", "-R", f"root:{proj_group}", str(project_dir)], capture_output=True)
            subprocess.run(["chmod", "770", str(project_dir)], capture_output=True)
            
            # Default ACLs so newly created files inherit rwx for group and none for others
            subprocess.run(["setfacl", "-d", "-m", "g::rwx", str(project_dir)], capture_output=True)
            subprocess.run(["setfacl", "-d", "-m", "o::---", str(project_dir)], capture_output=True)
            
            # Ensure Nextcloud daemon (www-data) can also read/write project directories if required
            subprocess.run(["setfacl", "-m", "u:www-data:rwx", str(project_dir)], capture_output=True)
            subprocess.run(["setfacl", "-d", "-m", "u:www-data:rwx", str(project_dir)], capture_output=True)
        except Exception as e:
            logger.debug(f"Failed to set project permissions on '{project_dir}': {e}")
            
        return str(project_dir)

    def apply_user_quota(self, user_id: int, soft_gb: int = None, hard_gb: int = None):
        linux_user = f"u{user_id}"
        if not self.user_exists(linux_user):
            return
            
        s_gb = soft_gb if soft_gb is not None else self.quota_soft_gb
        h_gb = hard_gb if hard_gb is not None else self.quota_hard_gb
        
        # We can import and reuse the existing quota_manager apply_quota function
        import quota_manager
        quota_manager.apply_quota(linux_user, s_gb, h_gb, str(self.users_path))
