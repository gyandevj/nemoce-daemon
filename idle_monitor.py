import time
import os
import logging
import threading
from pathlib import Path

logger = logging.getLogger("lab-daemon")

class IdleMonitor(threading.Thread):
    """
    A background thread that monitors sessions for inactivity.
    If a session has no file reads/writes (via st_atime / st_mtime) for
    more than configured timeout, it automatically triggers an unmount callback.
    """
    def __init__(self, session_manager, unmount_callback, idle_timeout_minutes: int, check_interval_seconds: int = 60):
        super().__init__()
        self.session_manager = session_manager
        self.unmount_callback = unmount_callback
        self.idle_timeout_seconds = idle_timeout_minutes * 60
        self.check_interval_seconds = check_interval_seconds
        self.daemon = True
        self._stop_event = threading.Event()

    def stop(self):
        """
        Stops the background thread gracefully.
        """
        self._stop_event.set()

    def run(self):
        logger.info(f"⏳ Idle Monitor: Thread started (Timeout: {self.idle_timeout_seconds // 60} minutes, Check Interval: {self.check_interval_seconds}s)")
        while not self._stop_event.is_set():
            self._stop_event.wait(self.check_interval_seconds)
            if self._stop_event.is_set():
                break
            
            try:
                self._check_sessions()
            except Exception as e:
                logger.error(f"❌ Idle Monitor: Error in checking cycle: {e}", exc_info=True)

    def _check_sessions(self):
        # Read all sessions from database
        sessions = self.session_manager.get_all_sessions()
        now = time.time()

        for session_id, session_info in list(sessions.items()):
            user = session_info.get("user")
            tool = session_info.get("tool")
            mount_points = session_info.get("mount_points", [])

            # Extract target session folders to scan
            target_folders = []
            for mp_str in mount_points:
                if "→" in mp_str:
                    target_folders.append(mp_str.split("→")[-1].strip())

            if not target_folders:
                continue

            max_activity = 0.0
            for folder in target_folders:
                folder_path = Path(folder)
                activity = self._get_last_activity(folder_path)
                if activity > max_activity:
                    max_activity = activity

            # Ensure max_activity is at least the mount_time of the session
            mount_time = float(session_info.get("mount_time", now))
            if max_activity < mount_time:
                max_activity = mount_time

            idle_time = now - max_activity
            if idle_time > self.idle_timeout_seconds:

                logger.info(f"⏳ Idle Monitor: Session '{session_id}' ({user} on {tool}) has been idle for {int(idle_time // 60)} minutes. Triggering auto-unmount.")
                try:
                    # Execute unmount callback
                    self.unmount_callback(user, tool, session_id)
                except Exception as e:
                    logger.error(f"❌ Idle Monitor: Failed to auto-unmount session '{session_id}': {e}")

    def _get_last_activity(self, folder_path: Path) -> float:
        """
        Finds the maximum access/modification time of any file/folder in the directory.
        """
        if not folder_path.exists():
            return 0.0
        
        try:
            # Check the base directory itself
            max_time = folder_path.stat().st_atime
            
            # Walk directory tree recursively to find latest read or write
            for root, dirs, files in os.walk(str(folder_path)):
                for name in dirs + files:
                    p = Path(root) / name
                    try:
                        stat_val = p.stat()
                        max_time = max(max_time, stat_val.st_atime, stat_val.st_mtime)
                    except Exception:
                        pass
            return max_time
        except Exception:
            return 0.0
