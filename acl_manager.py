import subprocess
import logging
from pathlib import Path

logger = logging.getLogger("lab-daemon")

def grant_group_access(username: str, group_dir: str) -> bool:
    """
    Grants read, write, execute (rwx) access to the specified user on the group directory
    using: setfacl -m u:username:rwx group_dir
    """
    group_dir_str = str(Path(group_dir).resolve())
    try:
        cmd = ["setfacl", "-m", f"u:{username}:rwx", group_dir_str]
        res = subprocess.run(cmd, capture_output=True, text=True, check=True)
        logger.info(f"🔑 ACL: Granted rwx access to user '{username}' on '{group_dir_str}'")
        return True
    except FileNotFoundError:
        logger.warning(f"⚠️ ACL Warning: 'setfacl' executable not found. Cannot set ACLs on '{group_dir_str}'.")
        return False
    except subprocess.CalledProcessError as e:
        logger.error(f"❌ ACL Error: Failed to grant access to user '{username}' on '{group_dir_str}': {e.stderr.strip()}")
        return False
    except Exception as e:
        logger.error(f"❌ ACL Error: Unexpected error: {e}")
        return False

def revoke_group_access(username: str, group_dir: str) -> bool:
    """
    Revokes access for the specified user from the group directory
    using: setfacl -x u:username group_dir
    """
    group_dir_str = str(Path(group_dir).resolve())
    try:
        cmd = ["setfacl", "-x", f"u:{username}", group_dir_str]
        res = subprocess.run(cmd, capture_output=True, text=True, check=True)
        logger.info(f"🔑 ACL: Revoked access for user '{username}' from '{group_dir_str}'")
        return True
    except FileNotFoundError:
        logger.warning(f"⚠️ ACL Warning: 'setfacl' executable not found. Cannot revoke ACLs on '{group_dir_str}'.")
        return False
    except subprocess.CalledProcessError as e:
        # Ignore errors if the entry doesn't exist, which can happen if it was already cleared
        if "no such entry" in e.stderr.lower():
            logger.info(f"🔑 ACL: User '{username}' already had no ACL entry on '{group_dir_str}'")
            return True
        logger.error(f"❌ ACL Error: Failed to revoke access for user '{username}' from '{group_dir_str}': {e.stderr.strip()}")
        return False
    except Exception as e:
        logger.error(f"❌ ACL Error: Unexpected error: {e}")
        return False
