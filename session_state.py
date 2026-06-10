import json
import os
import time
import logging

logger = logging.getLogger("lab-daemon")

# Try to import fcntl for Unix file locking, fall back to no-op on non-Unix platforms
try:
    import fcntl
except ImportError:
    fcntl = None

class SessionStateManager:
    """
    Manages active session data stored in a JSON file.
    Uses file locking to ensure concurrency safety.
    """
    def __init__(self, db_path: str):
        self.db_path = db_path
        # Ensure target parent directory exists
        db_dir = os.path.dirname(self.db_path)
        if db_dir:
            os.makedirs(db_dir, exist_ok=True)
        
        # Initialize file if not present
        if not os.path.exists(self.db_path):
            self._write_empty_db()

    def _write_empty_db(self):
        try:
            with open(self.db_path, "w") as f:
                json.dump({}, f, indent=2)
        except Exception as e:
            logger.error(f"Failed to initialize session database at '{self.db_path}': {e}")

    def _lock_file(self, f):
        if fcntl:
            try:
                fcntl.flock(f, fcntl.LOCK_EX)
            except IOError as e:
                logger.warning(f"Failed to acquire file lock: {e}")

    def _unlock_file(self, f):
        if fcntl:
            try:
                fcntl.flock(f, fcntl.LOCK_UN)
            except IOError as e:
                logger.warning(f"Failed to release file lock: {e}")

    def get_all_sessions(self) -> dict:
        """
        Reads and returns all active sessions from the database.
        """
        try:
            if not os.path.exists(self.db_path):
                return {}
            
            # Open in read-only mode, lock, load JSON, then unlock
            with open(self.db_path, "r") as f:
                self._lock_file(f)
                try:
                    data = json.load(f)
                except json.JSONDecodeError:
                    data = {}
                finally:
                    self._unlock_file(f)
                return data
        except Exception as e:
            logger.error(f"Error reading session database: {e}")
            return {}

    def save_session(self, session_id: str, user: str, tool: str, mount_points: list):
        """
        Saves or updates an active session in the database.
        """
        try:
            # Open in read-write mode, lock, update, truncate and write, then unlock
            mode = "r+" if os.path.exists(self.db_path) else "w+"
            with open(self.db_path, mode) as f:
                self._lock_file(f)
                try:
                    # Read existing contents
                    f.seek(0)
                    try:
                        content = f.read()
                        data = json.loads(content) if content else {}
                    except json.JSONDecodeError:
                        data = {}
                    
                    now = int(time.time())
                    # Persist session details
                    data[session_id] = {
                        "user": user,
                        "tool": tool,
                        "mount_time": now,
                        "last_heartbeat": now,
                        "mount_points": mount_points
                    }
                    
                    # Truncate and rewrite
                    f.seek(0)
                    f.truncate()
                    json.dump(data, f, indent=2)
                finally:
                    self._unlock_file(f)
            logger.info(f"💾 Session {session_id} saved to database")
        except Exception as e:
            logger.error(f"Error saving session {session_id} to database: {e}")

    def remove_session(self, session_id: str):
        """
        Removes a session from the database.
        """
        try:
            if not os.path.exists(self.db_path):
                return
            
            with open(self.db_path, "r+") as f:
                self._lock_file(f)
                try:
                    try:
                        data = json.load(f)
                    except json.JSONDecodeError:
                        data = {}
                    
                    if session_id in data:
                        del data[session_id]
                        f.seek(0)
                        f.truncate()
                        json.dump(data, f, indent=2)
                        logger.info(f"💾 Session {session_id} removed from database")
                    else:
                        logger.debug(f"Session {session_id} not found in database for removal")
                finally:
                    self._unlock_file(f)
        except Exception as e:
            logger.error(f"Error removing session {session_id} from database: {e}")

    def update_heartbeat(self, session_id: str):
        """
        Updates the last_heartbeat timestamp of an active session.
        """
        try:
            if not os.path.exists(self.db_path):
                return
            
            with open(self.db_path, "r+") as f:
                self._lock_file(f)
                try:
                    try:
                        data = json.load(f)
                    except json.JSONDecodeError:
                        data = {}
                    
                    if session_id in data:
                        data[session_id]["last_heartbeat"] = int(time.time())
                        f.seek(0)
                        f.truncate()
                        json.dump(data, f, indent=2)
                finally:
                    self._unlock_file(f)
        except Exception as e:
            logger.error(f"Error updating heartbeat for session {session_id}: {e}")
