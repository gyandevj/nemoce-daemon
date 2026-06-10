import subprocess
import logging
import os
from pathlib import Path

logger = logging.getLogger("lab-daemon")

def get_filesystem(path: str) -> str:
    """
    Finds the mount point or device filesystem of the directory path.
    Uses 'df -P path' to find the filesystem.
    """
    abs_path = str(Path(path).resolve())
    try:
        # Run: df -P path
        res = subprocess.run(["df", "-P", abs_path], capture_output=True, text=True, check=True)
        lines = res.stdout.strip().split("\n")
        if len(lines) > 1:
            parts = lines[1].split()
            # Under standard df -P, parts[5] is the mount point, parts[0] is the device filesystem.
            # setquota can take either. Mount point is usually more stable.
            return parts[5]
    except Exception as e:
        logger.debug(f"Could not determine filesystem for path '{abs_path}': {e}")
    # Fallback to the path itself
    return "/srv/labdata"

def apply_quota(username: str, soft_gb: float, hard_gb: float, path: str) -> bool:
    """
    Applies disk block quota limits to the specified user on the filesystem containing path.
    using: setquota -u username soft_blocks hard_blocks 0 0 filesystem
    Where blocks are in KB (1 block = 1 KB).
    """
    filesystem = get_filesystem(path)
    soft_kb = int(soft_gb * 1024 * 1024)
    hard_kb = int(hard_gb * 1024 * 1024)

    try:
        cmd = ["setquota", "-u", username, str(soft_kb), str(hard_kb), "0", "0", filesystem]
        subprocess.run(cmd, capture_output=True, text=True, check=True)
        logger.info(f"💾 Quota: Applied quota for user '{username}' on '{filesystem}' (Soft: {soft_gb}GB, Hard: {hard_gb}GB)")
        return True
    except FileNotFoundError:
        logger.warning(f"⚠️ Quota Warning: 'setquota' executable not found. Quota not applied to user '{username}'.")
        return False
    except subprocess.CalledProcessError as e:
        logger.error(f"❌ Quota Error: Failed to apply quota for user '{username}': {e.stderr.strip()}")
        return False
    except Exception as e:
        logger.error(f"❌ Quota Error: Unexpected error applying quota: {e}")
        return False

def check_quota_usage(username: str, path: str) -> dict:
    """
    Queries the disk quota usage and limits for the user.
    Returns a dictionary of usage information.
    """
    filesystem = get_filesystem(path)
    default_result = {
        "status": "unknown",
        "used_gb": 0.0,
        "soft_gb": 0.0,
        "hard_gb": 0.0,
        "exceeded": False
    }

    try:
        cmd = ["quota", "-u", username, "-w"]
        res = subprocess.run(cmd, capture_output=True, text=True)
        if res.returncode != 0:
            logger.debug(f"Quota command returned code {res.returncode} for user '{username}': {res.stderr.strip()}")
            return default_result

        # Parse output
        lines = res.stdout.strip().split("\n")
        for line in lines:
            # Look for lines containing the mount point filesystem
            if filesystem in line or (filesystem == "/" and " / " in line) or (filesystem == "/srv/labdata" and "labdata" in line):
                parts = line.split()
                # Find where the filesystem name is located
                try:
                    idx = -1
                    for i, part in enumerate(parts):
                        if filesystem in part or (filesystem == "/" and part == "/"):
                            idx = i
                            break
                    
                    if idx != -1 and len(parts) > idx + 3:
                        # Clean up '*' indicating quota exceeded
                        used_kb = int(parts[idx + 1].replace('*', ''))
                        soft_kb = int(parts[idx + 2])
                        hard_kb = int(parts[idx + 3])
                        
                        used_gb = round(used_kb / (1024 * 1024), 3)
                        soft_gb = round(soft_kb / (1024 * 1024), 3)
                        hard_gb = round(hard_kb / (1024 * 1024), 3)
                        
                        exceeded = (used_kb > soft_kb) if soft_kb > 0 else False
                        
                        return {
                            "status": "ok",
                            "used_gb": used_gb,
                            "soft_gb": soft_gb,
                            "hard_gb": hard_gb,
                            "exceeded": exceeded
                        }
                except ValueError:
                    continue
    except FileNotFoundError:
        logger.warning("⚠️ Quota Warning: 'quota' executable not found.")
    except Exception as e:
        logger.error(f"❌ Quota Error: Unexpected error querying quota: {e}")

    return default_result
