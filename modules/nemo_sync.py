import time
import logging
import threading
from modules.state_db import StateDB
from modules.nemo_api_client import NemoAPIClient
from modules.user_provisioner import UserProvisioner

logger = logging.getLogger("lab-daemon")

class NemoSync:
    """
    Periodically syncs NEMO accounts, projects, users, and memberships
    with local system directories, users, groups, quotas, and DB state.
    """
    def __init__(self, api_client: NemoAPIClient, db: StateDB, user_provisioner: UserProvisioner,
                 on_deactivation: str = "lock_account", poll_interval: int = 3600, dry_run: bool = False,
                 exclude_project_ids=None, exclude_account_ids=None,
                 exclude_project_names=None, exclude_account_names=None):
        self.api_client = api_client
        self.db = db
        self.user_provisioner = user_provisioner
        self.on_deactivation = on_deactivation
        self.poll_interval = poll_interval
        self.dry_run = dry_run
        
        self.exclude_project_ids = exclude_project_ids or []
        self.exclude_account_ids = exclude_account_ids or []
        self.exclude_project_names = exclude_project_names or []
        self.exclude_account_names = exclude_account_names or []
        
        self._running = False
        self._thread = None

    def _is_project_excluded(self, proj_id, proj_name, account_id) -> bool:
        # Check project ID
        if proj_id in self.exclude_project_ids:
            return True
        # Check project name (case-insensitive)
        p_names = [n.lower() for n in self.exclude_project_names]
        if proj_name and proj_name.lower() in p_names:
            return True
        # Check account ID
        if account_id in self.exclude_account_ids:
            return True
        # Check account name (case-insensitive)
        if account_id:
            acc_info = self.db.get_account_by_id(account_id)
            if acc_info:
                a_names = [n.lower() for n in self.exclude_account_names]
                if acc_info["name"] and acc_info["name"].lower() in a_names:
                    return True
        return False

    def _is_project_id_excluded(self, proj_id) -> bool:
        proj_info = self.db.get_project_by_id(proj_id)
        if not proj_info:
            return proj_id in self.exclude_project_ids
        
        proj_name = proj_info.get("name")
        account_id = proj_info.get("account_id")
        return self._is_project_excluded(proj_id, proj_name, account_id)


    def run_once(self):
        """
        Fetches all data from Django ORM and syncs local Linux state + SQLite DB.
        """
        logger.info("🔄 NemoSync: Starting synchronization run...")
        try:
            # 1. Fetch data
            accounts = self.api_client.get_accounts()
            projects = self.api_client.get_projects()
            users = self.api_client.get_users()
            
            logger.info(f"NemoSync: Fetched {len(accounts)} accounts, {len(projects)} projects, {len(users)} users.")

            if self.dry_run:
                logger.info("ℹ️ NemoSync: Dry-run active. No system changes will be applied.")

            # 2. Sync accounts
            self._sync_accounts(accounts)

            # 3. Sync projects
            self._sync_projects(projects)

            # 4. Sync users
            self._sync_users(users)

            # 5. Sync memberships
            self._sync_memberships(users)

            logger.info("🔄 NemoSync: Synchronization run completed successfully.")
        except Exception as e:
            logger.error(f"❌ NemoSync Error: Failed during sync run: {e}", exc_info=True)

    def _sync_accounts(self, accounts):
        for acc in accounts:
            acc_id = acc["id"]
            name = acc["name"]
            active = acc.get("active", True)
            
            if not self.dry_run:
                self.db.upsert_account(acc_id, name, active)

    def _sync_projects(self, projects):
        for proj in projects:
            proj_id = proj["id"]
            account_id = proj["account"]
            name = proj["name"]
            active = proj.get("active", True)
            
            linux_group = f"proj_{proj_id}"
            path = f"{self.user_provisioner.groups_path}/account_{account_id}/project_{proj_id}"
            
            if not self.dry_run:
                # Store in DB
                self.db.upsert_project(proj_id, account_id, name, linux_group, path, active)
                
                # Skip provisioning if the project or its account is excluded
                if self._is_project_excluded(proj_id, name, account_id):
                    logger.info(f"Skipping Linux group and directory provisioning for excluded project: '{name}' (ID: {proj_id})")
                    continue
                
                # Provision Linux group and project directory
                self.user_provisioner.provision_group(proj_id)
                if account_id:
                    self.user_provisioner.ensure_project_directory(account_id, proj_id)

    def _sync_users(self, users):
        for user in users:
            user_id = user["id"]
            username = user["username"]
            full_name = f"{user.get('first_name', '')} {user.get('last_name', '')}".strip()
            active = user.get("is_active", True)
            
            linux_user = f"u{user_id}"
            home_path = f"{self.user_provisioner.users_path}/{linux_user}"
            
            if not self.dry_run:
                # Store in DB
                self.db.upsert_user(
                    user_id=user_id,
                    username=username,
                    full_name=full_name,
                    active=active,
                    linux_user=linux_user,
                    home_path=home_path,
                    quota_soft_gb=self.user_provisioner.quota_soft_gb,
                    quota_hard_gb=self.user_provisioner.quota_hard_gb
                )
                # Provision Linux user and user home directory
                self.user_provisioner.provision_user(user_id, username)
                self.user_provisioner.ensure_user_directory(user_id)
                self.user_provisioner.apply_user_quota(user_id)
                # Set active status/locks
                self.user_provisioner.set_user_active_status(user_id, active, self.on_deactivation)

    def _sync_memberships(self, users):
        for user in users:
            user_id = user["id"]
            active = user.get("is_active", True)
            
            # If user is inactive and policy is to remove memberships, we clear them
            if not active and self.on_deactivation == "remove_membership_only":
                new_projects = set()
            else:
                new_projects = set(user.get("projects", []))
                
            # Filter out any projects that are excluded from group storage
            new_projects = {pid for pid in new_projects if not self._is_project_id_excluded(pid)}
            
            old_projects = self.db.get_user_projects(user_id)
            
            # Find differences
            to_add = new_projects - old_projects
            to_remove = old_projects - new_projects
            
            if not self.dry_run:
                # Add memberships
                for pid in to_add:
                    self.db.add_membership(user_id, pid)
                # Remove memberships
                for pid in to_remove:
                    self.db.remove_membership(user_id, pid)
                
                # Sync memberships to Linux user groups
                self.user_provisioner.sync_user_groups(user_id, list(new_projects))
                
                # Sync memberships to permanent Nextcloud group symlinks
                self._sync_user_my_groups_symlinks(user_id, list(new_projects))

    def _sync_user_my_groups_symlinks(self, user_id, project_ids):
        import os
        import re
        import subprocess
        from pathlib import Path
        
        def _safe_name(name: str) -> str:
            if not name:
                return name
            name = re.sub(r'[/\\:\*\?"<>\|]', '_', name)
            name = re.sub(r' +', ' ', name).strip()
            name = name.rstrip('.')
            reserved_patterns = re.compile(r'^(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])$', re.IGNORECASE)
            if reserved_patterns.match(name):
                name = f"_{name}"
            return name if name else "unnamed_folder"

        user_dir = Path(self.user_provisioner.users_path) / f"u{user_id}"
        if not user_dir.exists() or not user_dir.is_dir():
            return

        my_groups_dir = user_dir / "my_groups"
        try:
            my_groups_dir.mkdir(exist_ok=True)
            subprocess.run(["chown", "-R", f"u{user_id}:nogroup", str(my_groups_dir)], capture_output=True)
            subprocess.run(["chmod", "770", str(my_groups_dir)], capture_output=True)
        except Exception:
            pass

        expected_links = {}
        for pid in project_ids:
            proj_info = self.db.get_project_by_id(pid)
            if proj_info:
                safe_name = _safe_name(proj_info["name"])
                expected_links[safe_name] = proj_info["path"]

        try:
            for item in my_groups_dir.iterdir():
                if item.is_symlink():
                    link_name = item.name
                    if link_name not in expected_links:
                        logger.info(f"Removing stale group symlink: {item}")
                        item.unlink()
                    else:
                        try:
                            target = os.readlink(str(item))
                            if target != expected_links[link_name]:
                                logger.info(f"Recreating incorrect group symlink: {item} -> {expected_links[link_name]}")
                                item.unlink()
                        except Exception:
                            item.unlink()
        except Exception as e:
            logger.error(f"Error cleaning up stale symlinks in {my_groups_dir}: {e}")

        for link_name, target_path in expected_links.items():
            link_path = my_groups_dir / link_name
            if not link_path.exists() and not link_path.is_symlink():
                try:
                    logger.info(f"Creating group symlink: {link_path} -> {target_path}")
                    os.symlink(target_path, str(link_path))
                    try:
                        subprocess.run(["chown", "-h", f"u{user_id}:nogroup", str(link_path)], capture_output=True)
                    except Exception:
                        pass
                except Exception as e:
                    logger.warning(f"Failed to create group symlink {link_path}: {e}")


    # --- Background Loop Control ---

    def start(self):
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._run_loop, name="nemo-sync", daemon=True)
        self._thread.start()
        logger.info(f"NemoSync scheduler started in background thread (Interval: {self.poll_interval}s).")

    def stop(self):
        if not self._running:
            return
        self._running = False
        if self._thread:
            self._thread.join(timeout=2.0)
        logger.info("NemoSync scheduler stopped.")

    def _run_loop(self):
        # Initial wait of 10s to let system start up cleanly
        time.sleep(10.0)
        while self._running:
            self.run_once()
            # Sleep in 1-second chunks to react fast to shutdown
            for _ in range(self.poll_interval):
                if not self._running:
                    break
                time.sleep(1.0)
