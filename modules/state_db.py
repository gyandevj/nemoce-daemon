import sqlite3
import os
import time
import json
import logging

logger = logging.getLogger("lab-daemon")

try:
    import fcntl
except ImportError:
    fcntl = None

class StateDB:
    """
    SQLite database manager for lab-daemon state and active sessions.
    Enables WAL mode and applies flock for multi-process safety.
    """
    def __init__(self, db_path: str):
        self.db_path = db_path
        db_dir = os.path.dirname(self.db_path)
        if db_dir:
            os.makedirs(db_dir, exist_ok=True)
        self._init_db()

    def _get_connection(self):
        conn = sqlite3.connect(self.db_path, timeout=10.0)
        # Enable WAL mode
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA foreign_keys=ON;")
        conn.row_factory = sqlite3.Row
        return conn

    def _lock_db(self, conn):
        if fcntl:
            try:
                # We lock the database file exclusively to serialize writes if needed,
                # though SQLite WAL mode handles concurrent reads/writes well.
                # Since flock works on the file description, we get the file descriptor.
                fd = conn.fileno() if hasattr(conn, 'fileno') else None
                if not fd:
                    # Alternative: lock a separate lock file
                    lock_file_path = self.db_path + ".lock"
                    lock_file = open(lock_file_path, "w")
                    fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
                    # Store reference in connection object to avoid race conditions with multiple threads
                    conn._flock_file = lock_file
                else:
                    fcntl.flock(fd, fcntl.LOCK_EX)
            except Exception as e:
                logger.warning(f"Could not acquire DB lock: {e}")

    def _unlock_db(self, conn):
        if fcntl:
            try:
                fd = conn.fileno() if hasattr(conn, 'fileno') else None
                if not fd and hasattr(conn, '_flock_file'):
                    fcntl.flock(conn._flock_file.fileno(), fcntl.LOCK_UN)
                    conn._flock_file.close()
                elif fd:
                    fcntl.flock(fd, fcntl.LOCK_UN)
            except Exception as e:
                logger.warning(f"Could not release DB lock: {e}")

    def _init_db(self):
        conn = self._get_connection()
        self._lock_db(conn)
        try:
            with conn:
                # Accounts
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS accounts (
                        id INTEGER PRIMARY KEY,
                        name TEXT NOT NULL,
                        active INTEGER DEFAULT 1,
                        last_sync TIMESTAMP
                    );
                """)
                # Projects
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS projects (
                        id INTEGER PRIMARY KEY,
                        account_id INTEGER,
                        name TEXT NOT NULL,
                        linux_group TEXT UNIQUE NOT NULL,
                        path TEXT NOT NULL,
                        active INTEGER DEFAULT 1,
                        last_sync TIMESTAMP,
                        FOREIGN KEY (account_id) REFERENCES accounts(id) ON DELETE SET NULL
                    );
                """)
                # Users
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS users (
                        id INTEGER PRIMARY KEY,
                        username TEXT UNIQUE NOT NULL,
                        full_name TEXT,
                        active INTEGER DEFAULT 1,
                        linux_user TEXT UNIQUE NOT NULL,
                        home_path TEXT NOT NULL,
                        quota_soft_gb INTEGER DEFAULT 10,
                        quota_hard_gb INTEGER DEFAULT 12,
                        last_sync TIMESTAMP
                    );
                """)
                # Memberships
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS memberships (
                        user_id INTEGER,
                        project_id INTEGER,
                        PRIMARY KEY (user_id, project_id),
                        FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
                        FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE CASCADE
                    );
                """)
                # Active sessions
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS sessions (
                        session_id TEXT PRIMARY KEY,
                        user_id INTEGER,
                        tool_name TEXT NOT NULL,
                        mount_time TIMESTAMP,
                        last_heartbeat TIMESTAMP,
                        mount_points TEXT, -- JSON array
                        FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
                    );
                """)
            logger.info("Database initialized successfully.")
        except Exception as e:
            logger.error(f"Error initializing DB: {e}")
        finally:
            self._unlock_db(conn)
            conn.close()

    # --- Sync DB Operations ---

    def upsert_account(self, acc_id: int, name: str, active: bool):
        conn = self._get_connection()
        self._lock_db(conn)
        try:
            with conn:
                conn.execute("""
                    INSERT INTO accounts (id, name, active, last_sync)
                    VALUES (?, ?, ?, ?)
                    ON CONFLICT(id) DO UPDATE SET
                        name=excluded.name,
                        active=excluded.active,
                        last_sync=excluded.last_sync
                """, (acc_id, name, 1 if active else 0, int(time.time())))
        except Exception as e:
            logger.error(f"Failed to upsert account {acc_id}: {e}")
        finally:
            self._unlock_db(conn)
            conn.close()

    def upsert_project(self, proj_id: int, account_id: int, name: str, linux_group: str, path: str, active: bool):
        conn = self._get_connection()
        self._lock_db(conn)
        try:
            with conn:
                conn.execute("""
                    INSERT INTO projects (id, account_id, name, linux_group, path, active, last_sync)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(id) DO UPDATE SET
                        account_id=excluded.account_id,
                        name=excluded.name,
                        linux_group=excluded.linux_group,
                        path=excluded.path,
                        active=excluded.active,
                        last_sync=excluded.last_sync
                """, (proj_id, account_id, name, linux_group, path, 1 if active else 0, int(time.time())))
        except Exception as e:
            logger.error(f"Failed to upsert project {proj_id}: {e}")
        finally:
            self._unlock_db(conn)
            conn.close()

    def upsert_user(self, user_id: int, username: str, full_name: str, active: bool, linux_user: str, home_path: str, quota_soft_gb=10, quota_hard_gb=12):
        conn = self._get_connection()
        self._lock_db(conn)
        try:
            with conn:
                conn.execute("""
                    INSERT INTO users (id, username, full_name, active, linux_user, home_path, quota_soft_gb, quota_hard_gb, last_sync)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(id) DO UPDATE SET
                        username=excluded.username,
                        full_name=excluded.full_name,
                        active=excluded.active,
                        linux_user=excluded.linux_user,
                        home_path=excluded.home_path,
                        quota_soft_gb=excluded.quota_soft_gb,
                        quota_hard_gb=excluded.quota_hard_gb,
                        last_sync=excluded.last_sync
                """, (user_id, username, full_name, 1 if active else 0, linux_user, home_path, quota_soft_gb, quota_hard_gb, int(time.time())))
        except Exception as e:
            logger.error(f"Failed to upsert user {user_id}: {e}")
        finally:
            self._unlock_db(conn)
            conn.close()

    def add_membership(self, user_id: int, project_id: int):
        conn = self._get_connection()
        self._lock_db(conn)
        try:
            with conn:
                conn.execute("""
                    INSERT OR IGNORE INTO memberships (user_id, project_id)
                    VALUES (?, ?)
                """, (user_id, project_id))
        except Exception as e:
            logger.error(f"Failed to add membership user={user_id}, project={project_id}: {e}")
        finally:
            self._unlock_db(conn)
            conn.close()

    def remove_membership(self, user_id: int, project_id: int):
        conn = self._get_connection()
        self._lock_db(conn)
        try:
            with conn:
                conn.execute("""
                    DELETE FROM memberships WHERE user_id=? AND project_id=?
                """, (user_id, project_id))
        except Exception as e:
            logger.error(f"Failed to remove membership user={user_id}, project={project_id}: {e}")
        finally:
            self._unlock_db(conn)
            conn.close()

    def clear_user_memberships(self, user_id: int):
        conn = self._get_connection()
        self._lock_db(conn)
        try:
            with conn:
                conn.execute("DELETE FROM memberships WHERE user_id=?", (user_id,))
        except Exception as e:
            logger.error(f"Failed to clear memberships for user {user_id}: {e}")
        finally:
            self._unlock_db(conn)
            conn.close()

    def get_user_projects(self, user_id: int) -> set:
        conn = self._get_connection()
        try:
            cursor = conn.cursor()
            cursor.execute("SELECT project_id FROM memberships WHERE user_id=?", (user_id,))
            return {row[0] for row in cursor.fetchall()}
        except Exception as e:
            logger.error(f"Failed to query user projects for {user_id}: {e}")
            return set()
        finally:
            conn.close()

    def get_user_by_id(self, user_id: int) -> dict:
        conn = self._get_connection()
        try:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM users WHERE id=?", (user_id,))
            row = cursor.fetchone()
            return dict(row) if row else None
        except Exception as e:
            logger.error(f"Failed to query user by id {user_id}: {e}")
            return None
        finally:
            conn.close()

    def get_user_by_username(self, username: str) -> dict:
        conn = self._get_connection()
        try:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM users WHERE username=?", (username,))
            row = cursor.fetchone()
            return dict(row) if row else None
        except Exception as e:
            logger.error(f"Failed to query user by username {username}: {e}")
            return None
        finally:
            conn.close()

    def get_project_linux_groups(self, user_id: int) -> list:
        conn = self._get_connection()
        try:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT p.linux_group, p.id, p.account_id, p.path
                FROM projects p
                JOIN memberships m ON p.id = m.project_id
                WHERE m.user_id = ? AND p.active = 1
            """, (user_id,))
            return [dict(row) for row in cursor.fetchall()]
        except Exception as e:
            logger.error(f"Failed to query project Linux groups for user {user_id}: {e}")
            return []
        finally:
            conn.close()

    def get_user_projects_with_accounts(self, user_id: int) -> list:
        """Fetch all active projects and accounts associated with a user."""
        conn = self._get_connection()
        try:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT p.id as project_id, p.name as project_name, a.id as account_id, a.name as account_name
                FROM projects p
                JOIN accounts a ON p.account_id = a.id
                JOIN memberships m ON p.id = m.project_id
                WHERE m.user_id = ? AND p.active = 1 AND a.active = 1
            """, (user_id,))
            return [dict(row) for row in cursor.fetchall()]
        except Exception as e:
            logger.error(f"Failed to query user projects with accounts for user {user_id}: {e}")
            return []
        finally:
            conn.close()


    def get_project_by_id(self, project_id: int) -> dict:
        """Return project row (id, account_id, name, linux_group, path, active) or None."""
        conn = self._get_connection()
        try:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM projects WHERE id=?", (project_id,))
            row = cursor.fetchone()
            return dict(row) if row else None
        except Exception as e:
            logger.error(f"Failed to query project by id {project_id}: {e}")
            return None
        finally:
            conn.close()

    def get_account_by_id(self, account_id: int) -> dict:
        """Return account row (id, name, active) or None."""
        conn = self._get_connection()
        try:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM accounts WHERE id=?", (account_id,))
            row = cursor.fetchone()
            return dict(row) if row else None
        except Exception as e:
            logger.error(f"Failed to query account by id {account_id}: {e}")
            return None
        finally:
            conn.close()

    # --- Session DB Operations ---

    def save_session(self, session_id: str, user_id: int, tool_name: str, mount_points: list):
        conn = self._get_connection()
        self._lock_db(conn)
        try:
            now = int(time.time())
            mount_points_str = json.dumps(mount_points)
            with conn:
                conn.execute("""
                    INSERT INTO sessions (session_id, user_id, tool_name, mount_time, last_heartbeat, mount_points)
                    VALUES (?, ?, ?, ?, ?, ?)
                    ON CONFLICT(session_id) DO UPDATE SET
                        user_id=excluded.user_id,
                        tool_name=excluded.tool_name,
                        last_heartbeat=excluded.last_heartbeat,
                        mount_points=excluded.mount_points
                """, (session_id, user_id, tool_name, now, now, mount_points_str))
            logger.info(f"Session {session_id} saved to DB.")
        except Exception as e:
            logger.error(f"Failed to save session {session_id}: {e}")
        finally:
            self._unlock_db(conn)
            conn.close()

    def remove_session(self, session_id: str):
        conn = self._get_connection()
        self._lock_db(conn)
        try:
            with conn:
                conn.execute("DELETE FROM sessions WHERE session_id=?", (session_id,))
            logger.info(f"Session {session_id} removed from DB.")
        except Exception as e:
            logger.error(f"Failed to remove session {session_id}: {e}")
        finally:
            self._unlock_db(conn)
            conn.close()

    def clear_all_sessions(self) -> int:
        """Delete every session from the DB. Returns the number of rows deleted."""
        conn = self._get_connection()
        self._lock_db(conn)
        try:
            with conn:
                count = conn.execute("DELETE FROM sessions").rowcount
            logger.info(f"Cleared {count} session(s) from DB.")
            return count
        except Exception as e:
            logger.error(f"Failed to clear all sessions: {e}")
            return 0
        finally:
            self._unlock_db(conn)
            conn.close()

    def update_heartbeat(self, session_id: str):
        conn = self._get_connection()
        self._lock_db(conn)
        try:
            now = int(time.time())
            with conn:
                conn.execute("UPDATE sessions SET last_heartbeat=? WHERE session_id=?", (now, session_id))
        except Exception as e:
            logger.error(f"Failed to update heartbeat for {session_id}: {e}")
        finally:
            self._unlock_db(conn)
            conn.close()

    def get_all_sessions(self) -> dict:
        conn = self._get_connection()
        try:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT s.*, u.username as user
                FROM sessions s
                LEFT JOIN users u ON s.user_id = u.id
            """)
            sessions = {}
            for row in cursor.fetchall():
                sessions[row['session_id']] = {
                    "user_id": row['user_id'],
                    "user": row['user'] or f"u{row['user_id']}",
                    "tool": row['tool_name'],
                    "mount_time": row['mount_time'],
                    "last_heartbeat": row['last_heartbeat'],
                    "mount_points": json.loads(row['mount_points']) if row['mount_points'] else []
                }
            return sessions
        except Exception as e:
            logger.error(f"Failed to query all sessions: {e}")
            return {}
        finally:
            conn.close()
            
    def get_session(self, session_id: str) -> dict:
        conn = self._get_connection()
        try:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT s.*, u.username as user
                FROM sessions s
                LEFT JOIN users u ON s.user_id = u.id
                WHERE s.session_id = ?
            """, (session_id,))
            row = cursor.fetchone()
            if row:
                return {
                    "user_id": row['user_id'],
                    "user": row['user'] or f"u{row['user_id']}",
                    "tool": row['tool_name'],
                    "mount_time": row['mount_time'],
                    "last_heartbeat": row['last_heartbeat'],
                    "mount_points": json.loads(row['mount_points']) if row['mount_points'] else []
                }
            return None
        except Exception as e:
            logger.error(f"Failed to query session {session_id}: {e}")
            return None
        finally:
            conn.close()
