import os
import sys
import logging
import requests

logger = logging.getLogger("lab-daemon")

class NemoAPIClient:
    """
    NEMO API client that supports local Django ORM queries when available,
    and falls back to HTTP REST API queries when Django is not available or
    when configured to use HTTP.
    """
    def __init__(self, django_path: str = None, use_api_http: bool = False, api_url: str = None, api_token: str = None):
        self.django_path = django_path
        self.use_api_http = use_api_http
        self.api_url = api_url
        self.api_token = api_token
        self._django_initialized = False
        
        if not self.use_api_http and self.django_path:
            self._init_django()

    def _init_django(self):
        if self._django_initialized:
            return
        try:
            abs_path = os.path.abspath(self.django_path)
            if abs_path not in sys.path:
                sys.path.insert(0, abs_path)
            
            os.environ.setdefault("DJANGO_SETTINGS_MODULE", "settings")
            import django
            django.setup()
            
            # Re-enable stdout/stderr logging after Django reconfigures it
            root_logger = logging.getLogger("lab-daemon")
            if not root_logger.handlers:
                h = logging.StreamHandler(sys.stderr)
                h.setFormatter(logging.Formatter('%(asctime)s [%(levelname)s] %(message)s'))
                root_logger.addHandler(h)
                root_logger.setLevel(logging.INFO)
                
            self._django_initialized = True
            root_logger.info(f"Successfully initialized Django environment from path: {abs_path}")
        except Exception as e:
            logger.error(f"Failed to initialize Django environment in NemoAPIClient: {e}. Will attempt HTTP fallback if configured.")

    def _get_all_paginated(self, endpoint: str) -> list[dict]:
        if not self.api_url:
            logger.error("API URL is not configured. Cannot perform HTTP request.")
            return []
            
        headers = {}
        if self.api_token:
            headers["Authorization"] = f"Token {self.api_token}"
        
        results = []
        next_url = f"{self.api_url.rstrip('/')}{endpoint}"
        
        while next_url:
            try:
                resp = requests.get(next_url, headers=headers, timeout=10)
                if resp.status_code != 200:
                    logger.error(f"HTTP request failed to {next_url}: {resp.status_code} - {resp.text}")
                    break
                data = resp.json()
                if isinstance(data, dict) and "results" in data:
                    results.extend(data["results"])
                    next_url = data.get("next")
                elif isinstance(data, list):
                    results.extend(data)
                    next_url = None
                else:
                    logger.error(f"Unexpected response format from {next_url}: {data}")
                    break
            except Exception as e:
                logger.error(f"Error fetching paginated data from {next_url}: {e}")
                break
        return results

    def get_accounts(self) -> list[dict]:
        if self.use_api_http or not self._django_initialized:
            logger.info("Fetching accounts via HTTP REST API...")
            raw_accounts = self._get_all_paginated("/api/accounts/")
            accounts = []
            for acc in raw_accounts:
                accounts.append({
                    "id": acc["id"],
                    "name": acc["name"],
                    "active": acc.get("active", True)
                })
            return accounts

        try:
            from NEMO.models import Account
            accounts = []
            for acc in Account.objects.all():
                accounts.append({
                    "id": acc.id,
                    "name": acc.name,
                    "active": getattr(acc, "active", True)
                })
            return accounts
        except Exception as e:
            logger.error(f"Error fetching accounts from Django ORM: {e}")
            return []

    def get_projects(self) -> list[dict]:
        if self.use_api_http or not self._django_initialized:
            logger.info("Fetching projects via HTTP REST API...")
            raw_projects = self._get_all_paginated("/api/projects/")
            projects = []
            for proj in raw_projects:
                projects.append({
                    "id": proj["id"],
                    "account": proj.get("account"),
                    "name": proj["name"],
                    "active": proj.get("active", True)
                })
            return projects

        try:
            from NEMO.models import Project
            projects = []
            for proj in Project.objects.all().select_related('account'):
                projects.append({
                    "id": proj.id,
                    "account": proj.account.id if proj.account else None,
                    "name": proj.name,
                    "active": getattr(proj, "active", True)
                })
            return projects
        except Exception as e:
            logger.error(f"Error fetching projects from Django ORM: {e}")
            return []

    def get_users(self) -> list[dict]:
        if self.use_api_http or not self._django_initialized:
            logger.info("Fetching users via HTTP REST API...")
            raw_users = self._get_all_paginated("/api/users/")
            users = []
            for u in raw_users:
                users.append({
                    "id": u["id"],
                    "username": u["username"],
                    "first_name": u.get("first_name", ""),
                    "last_name": u.get("last_name", ""),
                    "is_active": u.get("is_active", True),
                    "projects": u.get("projects", [])
                })
            return users

        try:
            from NEMO.models import User
            users = []
            for u in User.objects.all().prefetch_related('projects'):
                users.append({
                    "id": u.id,
                    "username": u.username,
                    "first_name": u.first_name,
                    "last_name": u.last_name,
                    "is_active": u.is_active,
                    "projects": [p.id for p in u.projects.all()]
                })
            return users
        except Exception as e:
            logger.error(f"Error fetching users from Django ORM: {e}")
            return []
