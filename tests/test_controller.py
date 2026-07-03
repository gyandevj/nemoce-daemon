import os
import sys
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
  group_folder_type: "hierarchical"
  exclude_project_names: ["Buddy"]
  exclude_account_names: ["Administration"]

quota:
  default_soft: 10
  default_hard: 12

session:
  db_path: "{temp_db_path}"
  socket_path: "{tempfile.gettempdir()}/labdata/lab-daemon.sock"

nemo:
  django_path: "/mnt/c/Users/gyand/Desktop/NemoProject/nemo-ce"
  poll_interval_seconds: 3600

sync:
  on_deactivation: "lock_account"
  dry_run: true
"""

with open(temp_config_path, "w") as f:
    f.write(test_config_content)

# Set the environment variable before importing daemon_controller
os.environ["LAB_DAEMON_CONFIG"] = temp_config_path

# Add parent directory to path
parent_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if parent_dir not in sys.path:
    sys.path.insert(0, parent_dir)

# Import daemon_controller
import daemon_controller as controller

class TestController(unittest.TestCase):
    def setUp(self):
        # Populate DB with a test user, account, project
        controller.state_db.upsert_user(
            user_id=146,
            username="alex.abulnaga",
            full_name="Alex Abulnaga",
            active=True,
            linux_user="u146",
            home_path="/srv/labdata/users/u146"
        )
        controller.state_db.upsert_account(20, "Nathalie de Leon", True)
        controller.state_db.upsert_account(30, "Administration", True)
        controller.state_db.upsert_project(
            proj_id=426,
            account_id=20,
            name="C2QA",
            linux_group="proj_426",
            path="/srv/labdata/groups/account_20/project_426",
            active=True
        )
        controller.state_db.upsert_project(
            proj_id=500,
            account_id=30,
            name="Buddy",
            linux_group="proj_500",
            path="/srv/labdata/groups/account_30/project_500",
            active=True
        )
        controller.state_db.add_membership(146, 426)
        controller.state_db.add_membership(146, 500)

    def tearDown(self):
        # Clear database records
        conn = controller.state_db._get_connection()
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

    @patch('daemon_controller.quota_manager.check_quota_usage')
    @patch('daemon_controller.quota_manager.apply_quota')
    @patch('daemon_controller.acl_manager.grant_acl_access')
    @patch('daemon_controller._mount_bind')
    @patch('daemon_controller._mount_bind_ro')
    def test_handle_mount_success(self, mock_mount_ro, mock_mount, mock_acl, mock_apply_quota, mock_check_quota):
        mock_check_quota.return_value = {"exceeded": False}
        mock_acl.return_value = True

        payload = {
            "user_id": 146,
            "tool": "microscope1",
            "account_id": 20,
            "project_id": 426
        }

        res, code = controller.handle_mount(payload)
        self.assertEqual(code, 201)
        self.assertEqual(res["status"], "mounted")

        # Verify session saved in DB
        session_id = f"session_146_microscope1"
        session = controller.state_db.get_session(session_id)
        self.assertIsNotNone(session)
        self.assertEqual(session["user_id"], 146)

        # Verify bind mounts called twice (user + project)
        self.assertEqual(mock_mount.call_count, 2)

    @patch('daemon_controller.quota_manager.check_quota_usage')
    @patch('daemon_controller.quota_manager.apply_quota')
    @patch('daemon_controller.acl_manager.grant_acl_access')
    @patch('daemon_controller._mount_bind')
    @patch('daemon_controller._mount_bind_ro')
    def test_handle_mount_excluded_project(self, mock_mount_ro, mock_mount, mock_acl, mock_apply_quota, mock_check_quota):
        mock_check_quota.return_value = {"exceeded": False}
        mock_acl.return_value = True

        payload = {
            "user_id": 146,
            "tool": "microscope1",
            "account_id": 30,  # Administration (Excluded)
            "project_id": 500  # Buddy (Excluded)
        }

        res, code = controller.handle_mount(payload)
        self.assertEqual(code, 201)

        # Verify bind mounts called only ONCE (for user directory only, skipped project directory)
        self.assertEqual(mock_mount.call_count, 1)

    @patch('daemon_controller.acl_manager.revoke_acl_access')
    @patch('daemon_controller.graceful_unmount')
    @patch('daemon_controller._is_mountpoint')
    def test_handle_unmount(self, mock_is_mount, mock_unmount, mock_acl_revoke):
        mock_is_mount.return_value = True
        mock_unmount.return_value = True
        
        session_id = "session_146_microscope1"
        controller.state_db.save_session(session_id, 146, "microscope1", ["/srv/labdata/users/u146 → /tmp/labdata/sessions/microscope1/my_files"])

        payload = {
            "user_id": 146,
            "tool": "microscope1",
            "account_id": 20,
            "project_id": 426,
            "session_id": session_id
        }

        res, code = controller.handle_unmount(payload)
        self.assertEqual(code, 200)
        self.assertEqual(res["status"], "unmounted")

        # Verify session removed from DB
        self.assertIsNone(controller.state_db.get_session(session_id))


if __name__ == "__main__":
    unittest.main()
