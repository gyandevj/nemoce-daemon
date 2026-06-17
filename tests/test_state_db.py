import os
import tempfile
import unittest
from modules.state_db import StateDB

class TestStateDB(unittest.TestCase):
    def setUp(self):
        # Create a temporary database file
        self.db_fd, self.db_path = tempfile.mkstemp()
        self.state_db = StateDB(self.db_path)

    def tearDown(self):
        # Clean up database file
        os.close(self.db_fd)
        try:
            os.unlink(self.db_path)
            # Clean up WAL files
            if os.path.exists(self.db_path + "-wal"):
                os.unlink(self.db_path + "-wal")
            if os.path.exists(self.db_path + "-shm"):
                os.unlink(self.db_path + "-shm")
        except OSError:
            pass

    def test_init_db(self):
        # Verify tables are created
        conn = self.state_db._get_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table';")
        tables = [row[0] for row in cursor.fetchall()]
        self.assertIn("accounts", tables)
        self.assertIn("projects", tables)
        self.assertIn("users", tables)
        self.assertIn("memberships", tables)
        self.assertIn("sessions", tables)
        conn.close()

    def test_upsert_account(self):
        self.state_db.upsert_account(1, "Test Account", True)
        conn = self.state_db._get_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM accounts WHERE id=1;")
        row = cursor.fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(row["name"], "Test Account")
        self.assertEqual(row["active"], 1)
        conn.close()

    def test_upsert_project(self):
        self.state_db.upsert_account(1, "Test Account", True)
        self.state_db.upsert_project(10, 1, "Test Project", "proj_10", "/srv/labdata/groups/account_1/project_10", True)
        
        conn = self.state_db._get_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM projects WHERE id=10;")
        row = cursor.fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(row["name"], "Test Project")
        self.assertEqual(row["account_id"], 1)
        conn.close()

    def test_upsert_user_and_memberships(self):
        self.state_db.upsert_user(146, "alex", "Alex Abulnaga", True, "u146", "/srv/labdata/users/u146")
        user = self.state_db.get_user_by_id(146)
        self.assertIsNotNone(user)
        self.assertEqual(user["username"], "alex")

        # Provision project for membership tests
        self.state_db.upsert_account(1, "Test Account", True)
        self.state_db.upsert_project(426, 1, "C2QA", "proj_426", "/srv/labdata/groups/account_1/project_426", True)
        
        # Add membership
        self.state_db.add_membership(146, 426)
        projects = self.state_db.get_user_projects(146)
        self.assertIn(426, projects)
        
        # Test project linux groups mapping
        groups = self.state_db.get_project_linux_groups(146)
        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0]["linux_group"], "proj_426")

    def test_session_state(self):
        self.state_db.upsert_user(146, "alex", "Alex Abulnaga", True, "u146", "/srv/labdata/users/u146")
        self.state_db.save_session("session_146_microscope", 146, "microscope1", ["/srv/labdata/users/u146 -> /tmp/sessions/microscope1/my_files"])
        
        sessions = self.state_db.get_all_sessions()
        self.assertIn("session_146_microscope", sessions)
        self.assertEqual(sessions["session_146_microscope"]["user_id"], 146)
        self.assertEqual(sessions["session_146_microscope"]["tool"], "microscope1")

        # Test heartbeat
        prev_heartbeat = sessions["session_146_microscope"]["last_heartbeat"]
        import time
        time.sleep(1)
        self.state_db.update_heartbeat("session_146_microscope")
        session = self.state_db.get_session("session_146_microscope")
        self.assertGreater(session["last_heartbeat"], prev_heartbeat)

        # Remove session
        self.state_db.remove_session("session_146_microscope")
        self.assertIsNone(self.state_db.get_session("session_146_microscope"))

if __name__ == "__main__":
    unittest.main()
