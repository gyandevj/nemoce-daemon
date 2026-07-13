import os
import sys
import json
import unittest
import tempfile
from pathlib import Path
from unittest.mock import patch, MagicMock

# Create temporary files for the test configuration and SQLite database
temp_config_fd, temp_config_path = tempfile.mkstemp(suffix=".yaml")
temp_db_fd, temp_db_path = tempfile.mkstemp(suffix=".db")

# Use forward slashes for paths to prevent backslash parsing errors in PyYAML
temp_dir_safe = tempfile.gettempdir().replace("\\", "/")
temp_db_safe = temp_db_path.replace("\\", "/")
temp_config_safe = temp_config_path.replace("\\", "/")

os.close(temp_config_fd)
os.close(temp_db_fd)

# Write a sandboxed configuration for the test run
test_config_content = f"""
storage:
  base_path: "{temp_dir_safe}/labdata"
  users_path: "{temp_dir_safe}/labdata/users"
  groups_path: "{temp_dir_safe}/labdata/groups"
  sessions_path: "{temp_dir_safe}/labdata/sessions"
  public_path: "{temp_dir_safe}/labdata/public"
  group_folder_type: "hierarchical"

quota:
  default_soft: 10
  default_hard: 12

session:
  db_path: "{temp_db_safe}"
  socket_path: "{temp_dir_safe}/labdata/lab-daemon.sock"

nemo:
  django_path: "/mnt/c/Users/gyand/Desktop/NemoProject/nemo-ce"
  poll_interval_seconds: 3600

sync:
  on_deactivation: "lock_account"
  dry_run: true

mtls:
  enabled: true
  ca_cert: "certs/ca.crt"
  server_cert: "certs/server.crt"
  server_key: "certs/server.key"
"""

with open(temp_config_path, "w") as f:
    f.write(test_config_content)

# Set the environment variable before importing daemon_listener
os.environ["LAB_DAEMON_CONFIG"] = temp_config_safe

# Add parent directory to path
parent_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if parent_dir not in sys.path:
    sys.path.insert(0, parent_dir)

# Import daemon
import daemon as daemon
import importlib
importlib.reload(daemon)

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
        except OSError:
            pass

    def _generate_headers(self, success=True, body=None, timestamp=None, signature=None):
        headers = {
            'X-Client-Verify': 'SUCCESS' if success else 'NONE',
            'Content-Type': 'application/json'
        }
        
        # Always add valid or invalid HMAC signature headers
        import hmac
        import hashlib
        import time
        
        t_val = timestamp if timestamp else str(int(time.time()))
        SECRET_KEY = b"00d57012a01b31f8364ebdcda42f05d15c3fd5585c69be1b8cdec1c30caa3af7"
        
        raw_body = json.dumps(body).encode('utf-8') if body else b""
        message = f"{t_val}:".encode('utf-8') + raw_body
        
        sig_val = signature if signature else hmac.new(SECRET_KEY, message, hashlib.sha256).hexdigest()
        
        headers['X-Daemon-Signature'] = sig_val
        headers['X-Daemon-Timestamp'] = t_val
        
        return headers

    @patch('daemon.handle_quota')
    @patch('daemon.handle_unmount')
    @patch('daemon.handle_mount')
    def test_mount_unmount_flow(self, mock_mount, mock_unmount, mock_quota):
        tool = "microscope1"
        user_id = 146
        account_id = 20
        project_id = 426
        session_id = f"session_{user_id}_{tool}"

        # Mock side effects
        def mount_side_effect(payload):
            daemon.state_db.save_session(session_id, user_id, tool, [f"/srv/labdata/users/u146 → /tmp/labdata/sessions/microscope1/my_files"])
            return {"status": "mounted", "session_id": session_id}, 201
        mock_mount.side_effect = mount_side_effect

        def unmount_side_effect(payload):
            daemon.state_db.remove_session(session_id)
            return {"status": "unmounted"}, 200
        mock_unmount.side_effect = unmount_side_effect
        
        mock_quota.return_value = ({"used_gb": 5, "soft_limit_gb": 10}, 200)

        # 1. Test Mount Endpoint
        payload = {"user_id": user_id, "tool": tool, "account_id": account_id, "project_id": project_id}
        headers = self._generate_headers(success=True, body=payload)
        
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
        headers = self._generate_headers(success=True)
        resp = self.app.get("/sessions", headers=headers)
        self.assertEqual(resp.status_code, 200)
        data = json.loads(resp.data)
        self.assertIn(session_id, data)

        # 3. Test Quota Endpoint
        headers = self._generate_headers(success=True)
        resp = self.app.get("/quota/alex.abulnaga", headers=headers)
        self.assertEqual(resp.status_code, 200)
        data = json.loads(resp.data)
        self.assertEqual(data["used_gb"], 5)

        # 4. Test Unmount Endpoint
        payload = {"user_id": user_id, "tool": tool, "session_id": session_id, "account_id": account_id, "project_id": project_id}
        headers = self._generate_headers(success=True, body=payload)
        
        resp = self.app.post("/unmount", data=json.dumps(payload), headers=headers)
        self.assertEqual(resp.status_code, 200)
        data = json.loads(resp.data)
        self.assertEqual(data["status"], "unmounted")

        # Verify session removed from DB
        session = daemon.state_db.get_session(session_id)
        self.assertIsNone(session)

    @patch('daemon.handle_init_user')
    def test_init_user(self, mock_init):
        mock_init.return_value = ({"status": "initialized", "path": "/srv/labdata/users/u146"}, 200)
        
        user_id = 146
        payload = {"user_id": user_id}
        headers = self._generate_headers(success=True, body=payload)
        
        resp = self.app.post("/init_user", data=json.dumps(payload), headers=headers)
        self.assertEqual(resp.status_code, 200)
        data = json.loads(resp.data)
        self.assertEqual(data["status"], "initialized")
        self.assertEqual(data["path"], "/srv/labdata/users/u146")

    def test_unauthorized_access(self):
        payload = {"user_id": 146, "tool": "microscope1", "account_id": 20, "project_id": 426}
        
        # Test mount (invalid signature)
        headers = self._generate_headers(success=True, body=payload, signature="badsig")
        resp = self.app.post("/mount", data=json.dumps(payload), headers=headers)
        self.assertEqual(resp.status_code, 401)
        
        # Test unmount (missing headers)
        headers = {'Content-Type': 'application/json'}
        resp = self.app.post("/unmount", data=json.dumps(payload), headers=headers)
        self.assertEqual(resp.status_code, 401)
        
        # Test init_user (unauthorized verification check)
        # Verify headers works
        headers = self._generate_headers(success=False, body={"user_id": 146})
        headers['X-Client-Verify'] = 'NONE'
        daemon.config['mtls']['enabled'] = True
        try:
            resp = self.app.post("/init_user", data=json.dumps({"user_id": 146}), headers=headers)
            self.assertEqual(resp.status_code, 401)
        finally:
            daemon.config['mtls']['enabled'] = False


if __name__ == "__main__":
    unittest.main()
