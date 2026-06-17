import os
import unittest
import sys

# Add parent directory to path
parent_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if parent_dir not in sys.path:
    sys.path.insert(0, parent_dir)

from modules.nemo_api_client import NemoAPIClient

class TestNemoAPIClient(unittest.TestCase):
    def setUp(self):
        # We point to the local Django project directory
        self.django_path = "/mnt/c/Users/gyand/Desktop/NemoProject/nemo-ce"
        self.client = NemoAPIClient(self.django_path)

    def test_django_initialization(self):
        # The client should initialize successfully in WSL where Django is present
        # In a generic environment it might fail, but in this specific workspace it must succeed.
        if os.path.exists(self.django_path):
            self.assertTrue(self.client._django_initialized)

    def test_get_users(self):
        if self.client._django_initialized:
            users = self.client.get_users()
            self.assertIsInstance(users, list)
            if users:
                user = users[0]
                self.assertIn("id", user)
                self.assertIn("username", user)
                self.assertIn("projects", user)

    def test_get_projects(self):
        if self.client._django_initialized:
            projects = self.client.get_projects()
            self.assertIsInstance(projects, list)
            if projects:
                proj = projects[0]
                self.assertIn("id", proj)
                self.assertIn("account", proj)
                self.assertIn("name", proj)

    def test_get_accounts(self):
        if self.client._django_initialized:
            accounts = self.client.get_accounts()
            self.assertIsInstance(accounts, list)
            if accounts:
                acc = accounts[0]
                self.assertIn("id", acc)
                self.assertIn("name", acc)

if __name__ == "__main__":
    unittest.main()
