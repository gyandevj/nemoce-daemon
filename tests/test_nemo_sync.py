import os
import sys
import unittest
from unittest.mock import MagicMock, patch
import tempfile

# Add parent directory to path
parent_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if parent_dir not in sys.path:
    sys.path.insert(0, parent_dir)

from modules.state_db import StateDB
from modules.nemo_sync import NemoSync
from modules.user_provisioner import UserProvisioner

class TestNemoSync(unittest.TestCase):
    def setUp(self):
        self.db_fd, self.db_path = tempfile.mkstemp()
        self.db = StateDB(self.db_path)
        
        self.mock_api = MagicMock()
        self.mock_provisioner = MagicMock()
        
        # Setup mock return values
        self.mock_api.get_accounts.return_value = [
            {"id": 20, "name": "Nathalie de Leon", "active": True}
        ]
        self.mock_api.get_projects.return_value = [
            {"id": 426, "account": 20, "name": "C2QA-De Leon", "active": True}
        ]
        self.mock_api.get_users.return_value = [
            {"id": 146, "username": "alex.abulnaga", "first_name": "Alex", "last_name": "Abulnaga", "is_active": True, "projects": [426]}
        ]

        # Use temporary directories in mock provisioner
        self.mock_provisioner.groups_path = "/srv/labdata/groups"
        self.mock_provisioner.users_path = "/srv/labdata/users"
        self.mock_provisioner.quota_soft_gb = 10
        self.mock_provisioner.quota_hard_gb = 12

        self.sync = NemoSync(
            api_client=self.mock_api,
            db=self.db,
            user_provisioner=self.mock_provisioner,
            on_deactivation="lock_account",
            dry_run=False
        )

    def tearDown(self):
        os.close(self.db_fd)
        try:
            os.unlink(self.db_path)
        except OSError:
            pass

    def test_run_once(self):
        # Run sync once
        self.sync.run_once()

        # Check that DB was populated
        user = self.db.get_user_by_id(146)
        self.assertIsNotNone(user)
        self.assertEqual(user["username"], "alex.abulnaga")

        # Check membership populated
        projects = self.db.get_user_projects(146)
        self.assertIn(426, projects)

        # Verify provisioner was called
        self.mock_provisioner.provision_user.assert_called_with(146, "alex.abulnaga")
        self.mock_provisioner.ensure_user_directory.assert_called_with(146)
        self.mock_provisioner.ensure_project_directory.assert_called_with(20, 426)
        self.mock_provisioner.sync_user_groups.assert_called_with(146, [426])

    def test_excluded_projects(self):
        # Configure mocks to include an excluded project ("Buddy") under excluded account ("Administration")
        self.mock_api.get_accounts.return_value = [
            {"id": 20, "name": "Nathalie de Leon", "active": True},
            {"id": 30, "name": "Administration", "active": True}
        ]
        self.mock_api.get_projects.return_value = [
            {"id": 426, "account": 20, "name": "C2QA-De Leon", "active": True},
            {"id": 500, "account": 30, "name": "Buddy", "active": True}
        ]
        self.mock_api.get_users.return_value = [
            {"id": 146, "username": "alex.abulnaga", "first_name": "Alex", "last_name": "Abulnaga", "is_active": True, "projects": [426, 500]}
        ]

        # Reset mock call counts
        self.mock_provisioner.reset_mock()

        # Initialize sync with exclusions
        sync_with_excludes = NemoSync(
            api_client=self.mock_api,
            db=self.db,
            user_provisioner=self.mock_provisioner,
            on_deactivation="lock_account",
            dry_run=False,
            exclude_project_names=["Buddy"],
            exclude_account_names=["Administration"]
        )

        sync_with_excludes.run_once()

        # Check DB project upserts happened (we want projects in SQLite for state tracking)
        self.assertIsNotNone(self.db.get_project_by_id(426))
        self.assertIsNotNone(self.db.get_project_by_id(500))

        # Check that directory was provisioned for standard project
        self.mock_provisioner.ensure_project_directory.assert_any_call(20, 426)
        
        # Check that directory was NOT provisioned for the excluded project/account
        # We assert that ensure_project_directory was never called with (30, 500)
        for call_args in self.mock_provisioner.ensure_project_directory.call_args_list:
            self.assertNotEqual(call_args[0], (30, 500))

        # Verify that only project 426 (not 500) was synced to the Linux groups
        self.mock_provisioner.sync_user_groups.assert_called_with(146, [426])

    def test_my_groups_symlinks(self):
        import tempfile
        import os
        from pathlib import Path
        
        with tempfile.TemporaryDirectory() as temp_users_dir:
            self.mock_provisioner.users_path = temp_users_dir
            
            user_dir = Path(temp_users_dir) / "u146"
            user_dir.mkdir()
            
            self.db.upsert_account(20, "Nathalie de Leon", True)
            self.db.upsert_project(
                proj_id=426,
                account_id=20,
                name="C2QA-De Leon",
                linux_group="proj_426",
                path="/srv/labdata/groups/account_20/project_426",
                active=True
            )
            
            self.sync._sync_user_my_groups_symlinks(146, [426])
            
            my_groups_dir = user_dir / "my_groups"
            self.assertTrue(my_groups_dir.exists())
            
            symlink_path = my_groups_dir / "C2QA-De Leon"
            self.assertTrue(symlink_path.is_symlink())
            self.assertEqual(os.readlink(str(symlink_path)), "/srv/labdata/groups/account_20/project_426")
            
            self.sync._sync_user_my_groups_symlinks(146, [])
            self.assertFalse(symlink_path.exists())


if __name__ == "__main__":
    unittest.main()

