import os
import sys
import unittest
import tempfile
from pathlib import Path

# Add parent directory to path
parent_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if parent_dir not in sys.path:
    sys.path.insert(0, parent_dir)

from modules.user_provisioner import UserProvisioner

class TestUserProvisioner(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        base_path = Path(self.temp_dir.name)
        self.users_path = base_path / "users"
        self.groups_path = base_path / "groups"
        self.users_path.mkdir(exist_ok=True)
        self.groups_path.mkdir(exist_ok=True)
        
        self.provisioner = UserProvisioner(
            base_path=str(base_path),
            users_path=str(self.users_path),
            groups_path=str(self.groups_path)
        )

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_ensure_user_directory(self):
        # We test ensure_user_directory creates user subfolder
        # Note: on Windows, chown/setfacl calls will be caught/ignored in our code, which is expected behavior.
        user_dir = self.provisioner.ensure_user_directory(146)
        expected_path = self.users_path / "u146"
        self.assertEqual(user_dir, str(expected_path))
        self.assertTrue(expected_path.exists())
        self.assertTrue(expected_path.is_dir())

    def test_ensure_project_directory(self):
        # Tests folder structure account_X/project_Y is created
        project_dir = self.provisioner.ensure_project_directory(20, 426)
        expected_path = self.groups_path / "account_20" / "project_426"
        self.assertEqual(project_dir, str(expected_path))
        self.assertTrue(expected_path.exists())
        self.assertTrue(expected_path.is_dir())
        self.assertTrue(expected_path.parent.exists())

    def test_group_user_checks_dont_crash(self):
        # Checks that grp/pwd check functions work without exceptions
        res1 = self.provisioner.group_exists("root")
        res2 = self.provisioner.user_exists("root")
        # Should not crash
        self.assertIsInstance(res1, bool)
        self.assertIsInstance(res2, bool)

if __name__ == "__main__":
    unittest.main()
