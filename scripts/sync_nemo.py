#!/usr/bin/env python3
"""
CLI script to run NEMO synchronization manually.
"""
import os
import sys
import argparse
import logging

# Set up simple logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
logger = logging.getLogger("sync_nemo")

# Add parent directory to path
parent_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if parent_dir not in sys.path:
    sys.path.insert(0, parent_dir)

from modules.state_db import StateDB
from modules.nemo_api_client import NemoAPIClient
from modules.user_provisioner import UserProvisioner
from modules.nemo_sync import NemoSync

def main():
    parser = argparse.ArgumentParser(description="Synchronize NEMO accounts, projects, and users to Linux system.")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--dry-run", action="store_true", help="Preview changes without making any system or database updates")
    group.add_argument("--apply", action="store_true", help="Apply changes and provision users/groups/directories")
    
    args = parser.parse_args()

    import yaml
    config_path = os.path.join(parent_dir, "config.yaml")
    
    # Load configuration
    try:
        with open(config_path, "r") as f:
            config = yaml.safe_load(f)
    except Exception as e:
        logger.error(f"Failed to load configuration from {config_path}: {e}")
        sys.exit(1)

    django_path = config["nemo"]["django_path"]
    sqlite_db_path = config["session"]["db_path"]
    
    on_deactivation = config.get("sync", {}).get("on_deactivation", "lock_account")
    
    storage_cfg = config["storage"]
    quota_cfg = config["quota"]

    logger.info("Initializing NEMO sync client...")
    
    api_client = NemoAPIClient(
        django_path=django_path,
        use_api_http=config["nemo"].get("use_api_http", False),
        api_url=config["nemo"].get("api_url"),
        api_token=config["nemo"].get("api_token")
    )
    
    # 2. Initialize SQLite DB
    db = StateDB(sqlite_db_path)
    
    # 3. Initialize User Provisioner
    user_provisioner = UserProvisioner(
        base_path=storage_cfg["base_path"],
        users_path=storage_cfg["users_path"],
        groups_path=storage_cfg["groups_path"],
        quota_soft_gb=quota_cfg["default_soft"],
        quota_hard_gb=quota_cfg["default_hard"]
    )
    
    # 4. Initialize NemoSync
    sync = NemoSync(
        api_client=api_client,
        db=db,
        user_provisioner=user_provisioner,
        on_deactivation=on_deactivation,
        dry_run=args.dry_run
    )
    
    # Run sync once
    sync.run_once()

if __name__ == "__main__":
    main()
