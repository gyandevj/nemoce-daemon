import os
import sys
import json
import time
import hmac
import hashlib
import unittest
import tempfile
from pathlib import Path
from unittest.mock import patch, MagicMock

# Create temporary files for the test configuration and SQLite database
temp_config_fd, temp_config_path = tempfile.mkstemp(suffix=".yaml")
temp_db_fd, temp_db_path = tempfile.mkstemp(suffix=".db")

os.close(temp_config_fd)
os.close(temp_db_fd)

# Write a sandboxed configuration for the test run
test_config_content = f"""
storage:
  base_path: "{tempfile.gettempdir()}/labdata"
  users_path: "{tempfile.gettempdir()}/labdata/users"
  groups_path: "{tempfile.gettempdir()}/labdata/groups"
  sessions_path: "{tempfile.gettempdir()}/labdata/sessions"
  public_path: "{tempfile.gettempdir()}/labdata/public"

quota:
  default_soft: 10
  default_hard: 12

session:
  db_path: "{temp_db_path}"
  idle_timeout_minutes: 60
  unmount_grace_seconds: 30

nemo:
  django_path: "/mnt/c/Users/gyand/Desktop/NemoProject/nemo-ce"
  poll_interval_seconds: 3600

sync:
  on_deactivation: "lock_account"
  dry_run: true
"""

with open(temp_config_path, "w") as f:
    f.write(test_config_content)

# Set the environment variable before importing daemon, so it uses this configuration
os.environ["LAB_DAEMON_CONFIG"] = temp_config_path

# Add parent directory to path
parent_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if parent_dir not in sys.path:
    sys.path.insert(0, parent_dir)

# Import daemon now that the configuration is mocked
import daemon

# Override SECRET_KEY and disable real libc mount calls
daemon.SECRET_KEY = b"test_secret_key"
daemon._libc = None


class TestIntegration(unittest.TestCase):
    def setUp(self):
        self.app = daemon.app.test_client()
        self.app.testing = True
        
        # Populate DB with a test user & project
        daemon.state_db.upsert_user(
            user_id=146,
            username="alex.abulnaga",
            full_name="Alex Abulnaga",
            active=True,
            linux_user="u146",
            home_path="/srv/labdata/users/u146"
        )
        daemon.state_db.upsert_account(20, "Nathalie de Leon", True)
        daemon.state_db.upsert_project(
            proj_id=426,
            account_id=20,
            name="C2QA",
            linux_group="proj_426",
            path="/srv/labdata/groups/account_20/project_426",
            active=True
        )
        daemon.state_db.add_membership(146, 426)

    def tearDown(self):
        # Clear database records
        conn = daemon.state_db._get_connection()
        with conn:
            conn.execute("DELETE FROM sessions;")
            conn.execute("DELETE FROM memberships;")
            conn.execute("DELETE FROM projects;")
            conn.execute("DELETE FROM accounts;")
            conn.execute("DELETE FROM users;")
        conn.close()

    @classmethod
    def tearDownClass(cls):
        # Remove temporary DB and config files
        try:
            os.unlink(temp_config_path)
            os.unlink(temp_db_path)
            # Remove WAL files if present
            if os.path.exists(temp_db_path + "-wal"):
                os.unlink(temp_db_path + "-wal")
            if os.path.exists(temp_db_path + "-shm"):
                os.unlink(temp_db_path + "-shm")
        except OSError:
            pass

    def _generate_headers(self, user_id_str, tool):
        timestamp = str(int(time.time()))
        message = f"{user_id_str}{tool}{timestamp}"
        signature = hmac.new(
            daemon.SECRET_KEY,
            message.encode('utf-8'),
            hashlib.sha256
        ).hexdigest()
        
        return {
            'X-Timestamp': timestamp,
            'X-Signature': signature,
            'Content-Type': 'application/json'
        }

    @patch('daemon.quota_manager.check_quota_usage')
    @patch('daemon.quota_manager.apply_quota')
    @patch('daemon.acl_manager.grant_acl_access')
    @patch('daemon._mount_bind')
    @patch('daemon._mount_bind_ro')
    def test_mount_unmount_flow(self, mock_mount_ro, mock_mount, mock_acl, mock_apply_quota, mock_check_quota):
        mock_check_quota.return_value = {"exceeded": False}
        mock_acl.return_value = True

        tool = "microscope1"
        user_id = 146
        user_id_str = "146"
        session_id = f"session_{user_id}_{tool}"

        # 1. Test Mount Endpoint
        headers = self._generate_headers(user_id_str, tool)
        payload = {"user_id": user_id, "tool": tool}
        
        resp = self.app.post("/mount", data=json.dumps(payload), headers=headers)
        self.assertEqual(resp.status_code, 201)
        data = json.loads(resp.data)
        self.assertEqual(data["status"], "mounted")
        self.assertEqual(data["session_id"], session_id)

        # Verify session saved in DB
        session = daemon.state_db.get_session(session_id)
        self.assertIsNotNone(session)
        self.assertEqual(session["user_id"], user_id)
        self.assertEqual(session["tool"], tool)

        # 2. Test Get Sessions Endpoint
        resp = self.app.get("/sessions")
        self.assertEqual(resp.status_code, 200)
        data = json.loads(resp.data)
        self.assertIn(session_id, data)

        # 3. Test Unmount Endpoint
        headers = self._generate_headers(user_id_str, tool)
        payload = {"user_id": user_id, "tool": tool, "session_id": session_id}
        
        resp = self.app.post("/unmount", data=json.dumps(payload), headers=headers)
        self.assertEqual(resp.status_code, 200)
        data = json.loads(resp.data)
        self.assertEqual(data["status"], "unmounted")

        # Verify session removed from DB
        session = daemon.state_db.get_session(session_id)
        self.assertIsNone(session)

    @patch('daemon.user_provisioner.ensure_user_directory')
    @patch('daemon.user_provisioner.apply_user_quota')
    def test_init_user(self, mock_quota, mock_dir):
        mock_dir.return_value = "/srv/labdata/users/u146"
        
        user_id = 146
        user_id_str = "146"
        headers = self._generate_headers(user_id_str, "system")
        payload = {"user_id": user_id}
        
        resp = self.app.post("/init_user", data=json.dumps(payload), headers=headers)
        self.assertEqual(resp.status_code, 200)
        data = json.loads(resp.data)
        self.assertEqual(data["status"], "initialized")
        self.assertEqual(data["path"], "/srv/labdata/users/u146")


if __name__ == "__main__":
    unittest.main()
