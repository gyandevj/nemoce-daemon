import subprocess
import logging
from pathlib import Path

logger = logging.getLogger("lab-daemon")

def grant_acl_access(username: str, path: str, permissions: str = "rwx") -> bool:
    """
    Grants specific ACL permissions to a user or machine account on a target directory.
    using: setfacl -m u:username:permissions path
    """
    resolved_path = str(Path(path).resolve())
    try:
        cmd = ["setfacl", "-m", f"u:{username}:{permissions}", resolved_path]
        subprocess.run(cmd, capture_output=True, text=True, check=True)
        logger.info(f"🔑 ACL: Granted '{permissions}' access to '{username}' on '{resolved_path}'")
        return True
    except FileNotFoundError:
        logger.warning(f"⚠️ ACL Warning: 'setfacl' executable not found. Cannot set ACLs on '{resolved_path}'.")
        return False
    except subprocess.CalledProcessError as e:
        logger.error(f"❌ ACL Error: Failed to grant access to '{username}' on '{resolved_path}': {e.stderr.strip()}")
        return False
    except Exception as e:
        logger.error(f"❌ ACL Error: Unexpected error: {e}")
        return False

def revoke_acl_access(username: str, path: str) -> bool:
    """
    Revokes ACL permissions for a user or machine account from a target directory.
    using: setfacl -x u:username path
    """
    resolved_path = str(Path(path).resolve())
    try:
        cmd = ["setfacl", "-x", f"u:{username}", resolved_path]
        subprocess.run(cmd, capture_output=True, text=True, check=True)
        logger.info(f"🔑 ACL: Revoked access for '{username}' from '{resolved_path}'")
        return True
    except FileNotFoundError:
        logger.warning(f"⚠️ ACL Warning: 'setfacl' executable not found. Cannot revoke ACLs on '{resolved_path}'.")
        return False
    except subprocess.CalledProcessError as e:
        # Ignore errors if the entry doesn't exist, which can happen if it was already cleared
        if "no such entry" in e.stderr.lower():
            logger.info(f"🔑 ACL: '{username}' already had no ACL entry on '{resolved_path}'")
            return True
        logger.error(f"❌ ACL Error: Failed to revoke access for '{username}' from '{resolved_path}': {e.stderr.strip()}")
        return False
    except Exception as e:
        logger.error(f"❌ ACL Error: Unexpected error: {e}")
        return False

def grant_group_access(username: str, group_dir: str) -> bool:
    """Backward compatibility wrapper"""
    return grant_acl_access(username, group_dir, "rwx")

def revoke_group_access(username: str, group_dir: str) -> bool:
    """Backward compatibility wrapper"""
    return revoke_acl_access(username, group_dir)
