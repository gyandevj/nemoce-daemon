#!/usr/bin/env python3
"""
Migrates legacy JSON session data to the SQLite state database.
"""
import os
import sys
import json
import logging

# Set up simple logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
logger = logging.getLogger("migration")

# Add parent directory and modules to path
parent_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if parent_dir not in sys.path:
    sys.path.insert(0, parent_dir)

from modules.state_db import StateDB
from modules.nemo_api_client import NemoAPIClient

def main():
    import yaml
    config_path = os.path.join(parent_dir, "config.yaml")
    
    # Load configuration
    try:
        with open(config_path, "r") as f:
            config = yaml.safe_load(f)
    except Exception as e:
        logger.error(f"Failed to load configuration from {config_path}: {e}")
        sys.exit(1)

    legacy_json_path = "/var/lib/lab-daemon/sessions.json"
    sqlite_db_path = config["session"]["db_path"]
    django_path = config["nemo"]["django_path"]

    logger.info(f"Starting migration from legacy JSON: {legacy_json_path}")
    logger.info(f"Target SQLite DB: {sqlite_db_path}")

    if not os.path.exists(legacy_json_path):
        logger.warning(f"Legacy JSON file '{legacy_json_path}' does not exist. Nothing to migrate.")
        # We still initialize the DB
        db = StateDB(sqlite_db_path)
        logger.info("Empty SQLite database initialized.")
        sys.exit(0)

    try:
        with open(legacy_json_path, "r") as f:
            legacy_data = json.load(f)
    except Exception as e:
        logger.error(f"Failed to read legacy JSON file: {e}")
        sys.exit(1)

    api_client = NemoAPIClient(
        django_path=django_path,
        use_api_http=config["nemo"].get("use_api_http", False),
        api_url=config["nemo"].get("api_url"),
        api_token=config["nemo"].get("api_token")
    )
    users = api_client.get_users()
    username_to_id = {u["username"]: u["id"] for u in users}
    
    logger.info(f"Loaded {len(username_to_id)} user mappings from Django.")

    # Initialize SQLite database
    db = StateDB(sqlite_db_path)

    migrated_count = 0
    for session_id, s_info in legacy_data.items():
        username = s_info.get("user")
        tool = s_info.get("tool")
        mount_time = s_info.get("mount_time")
        last_heartbeat = s_info.get("last_heartbeat")
        mount_points = s_info.get("mount_points", [])

        # Look up user ID
        user_id = username_to_id.get(username)
        if not user_id:
            logger.warning(f"Username '{username}' not found in Django database. Creating a mock record in SQLite users.")
            # Generate a mock user ID for consistency if not found
            # We use a hash of the username as a negative number or something distinct
            import hashlib
            user_id = int(hashlib.md5(username.encode()).hexdigest(), 16) % 10000000 + 9000000
            # Upsert user in state_db so foreign key matches
            db.upsert_user(
                user_id=user_id,
                username=username,
                full_name=f"Legacy {username}",
                active=True,
                linux_user=f"u{user_id}",
                home_path=f"/srv/labdata/users/u{user_id}"
            )
            username_to_id[username] = user_id
        else:
            # Upsert active user details to ensure foreign key constraint is happy
            db.upsert_user(
                user_id=user_id,
                username=username,
                full_name=username,
                active=True,
                linux_user=f"u{user_id}",
                home_path=f"/srv/labdata/users/u{user_id}"
            )

        logger.info(f"Migrating session {session_id} for user {username} (ID: {user_id}) on tool {tool}")
        
        # Save session to SQLite
        db.save_session(session_id, user_id, tool, mount_points)
        migrated_count += 1

    logger.info(f"Successfully migrated {migrated_count} sessions to SQLite state database.")

if __name__ == "__main__":
    main()
