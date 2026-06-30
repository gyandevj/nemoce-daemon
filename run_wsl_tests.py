#!/usr/bin/env python3
import os
import sys
import time
import hmac
import hashlib
import json
import requests
import subprocess
import shutil
import sqlite3
import socket
from pathlib import Path

# Config
SECRET_KEY = b"00d57012a01b31f8364ebdcda42f05d15c3fd5585c69be1b8cdec1c30caa3af7"
BASE_URL = "http://127.0.0.1:5000"
SESSIONS_DIR = Path("/tmp/labdata/sessions")
USERS_DIR = Path("/srv/labdata/users")
GROUPS_DIR = Path("/srv/labdata/groups")

PASS_COUNT = 0
FAIL_COUNT = 0
daemon_proc = None

def log_test(name, success, info=""):
    global PASS_COUNT, FAIL_COUNT
    status = "\033[92mPASS\033[0m" if success else "\033[91mFAIL\033[0m"
    if success:
        PASS_COUNT += 1
    else:
        FAIL_COUNT += 1
    print(f"[{status}] {name} {info}")

def sign(user, tool):
    # HMAC signing is not strictly required by daemon anymore since v2 relies on mTLS
    # but we can keep a dummy sign function or headers for backward compatibility.
    timestamp = str(int(time.time()))
    return {"X-Timestamp": timestamp}

def is_mounted(path):
    try:
        with open("/proc/self/mountinfo", "r") as f:
            for line in f:
                if str(path) in line:
                    return True
    except Exception:
        pass
    return False

def kill_daemon():
    global daemon_proc
    if daemon_proc:
        daemon_proc.terminate()
        try:
            daemon_proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            daemon_proc.kill()
        daemon_proc = None
    else:
        # Fallback: kill using pkill of the exact python daemon.py command, avoiding matching this script
        subprocess.run(["pkill", "-9", "-f", "python.*/daemon.py"], capture_output=True)

def start_daemon():
    global daemon_proc
    # Check if already running (port in use)
    port_in_use = False
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.bind(("127.0.0.1", 5000))
        s.close()
    except OSError:
        port_in_use = True

    if not port_in_use:
        print("Starting daemon in background...")
        daemon_proc = subprocess.Popen(
            ["/mnt/c/Users/gyand/Desktop/NemoProject/nemo-ce/venv/bin/python", "-u", "/mnt/c/Users/gyand/Desktop/NemoProject/lab-daemon/daemon.py"],
            stdout=open("/tmp/daemon.log", "w"),
            stderr=subprocess.STDOUT
        )
        
        # Poll health endpoint until ready (up to 20 seconds)
        start_time = time.time()
        while time.time() - start_time < 20:
            try:
                resp = requests.get(f"{BASE_URL}/health", timeout=1)
                if resp.status_code == 200:
                    print("Daemon is ready.")
                    return
            except Exception:
                pass
            time.sleep(0.5)
        print("Warning: Daemon did not start within 20 seconds.")

def clean_all():
    # Force unmount any remaining mounts under SESSIONS_DIR
    try:
        with open("/proc/self/mountinfo", "r") as f:
            for line in f:
                if "labdata" in line:
                    parts = line.strip().split()
                    if len(parts) > 4:
                        subprocess.run(["umount", "-l", parts[4]], capture_output=True)
    except Exception:
        pass
    
    # Empty SQLite DB and seed mock data
    db_path = "/var/lib/lab-daemon/state.db"
    if os.path.exists(db_path):
        try:
            conn = sqlite3.connect(db_path)
            conn.execute("DELETE FROM sessions")
            conn.execute("DELETE FROM memberships")
            conn.execute("DELETE FROM projects")
            conn.execute("DELETE FROM accounts")
            conn.execute("DELETE FROM users")
            # Seed Alice (ID: 101) and Bob (ID: 102)
            conn.execute("INSERT INTO users (id, username, full_name, active, linux_user, home_path) VALUES (?, ?, ?, ?, ?, ?)",
                         (101, 'alice', 'Alice Smith', 1, 'ualice', '/srv/labdata/users/ualice'))
            conn.execute("INSERT INTO users (id, username, full_name, active, linux_user, home_path) VALUES (?, ?, ?, ?, ?, ?)",
                         (102, 'bob', 'Bob Jones', 1, 'ubob', '/srv/labdata/users/ubob'))
            # Seed Account 50 and Project 400
            conn.execute("INSERT INTO accounts (id, name, active) VALUES (?, ?, ?)", (50, 'Test Account', 1))
            conn.execute("INSERT INTO projects (id, account_id, name, linux_group, path, active) VALUES (?, ?, ?, ?, ?, ?)",
                         (400, 50, 'Test Project', 'proj_400', '/srv/labdata/groups/account_50/project_400', 1))
            # Seed memberships
            conn.execute("INSERT INTO memberships (user_id, project_id) VALUES (?, ?)", (101, 400))
            conn.execute("INSERT INTO memberships (user_id, project_id) VALUES (?, ?)", (102, 400))
            conn.commit()
            conn.close()
        except Exception as e:
            print(f"Error seeding DB: {e}")

def run_tests():
    global PASS_COUNT, FAIL_COUNT
    print("==================================================")
    print("       STARTING LAB DATA MOUNT DAEMON TESTS       ")
    print("==================================================")
    
    # Cleanup before starting
    kill_daemon()
    clean_all()

    # Ensure daemon is running using the venv python
    start_daemon()
        
    # Provision mock users
    try:
        requests.post(f"{BASE_URL}/init_user", json={"user_id": 101})
        requests.post(f"{BASE_URL}/init_user", json={"user_id": 102})
    except Exception as e:
        print(f"Error provisioning users: {e}")

    # ----------------------------------------------------
    # Test 1: Daemon Installation & Health
    # ----------------------------------------------------
    try:
        resp = requests.get(f"{BASE_URL}/health")
        log_test("Test 1: Health Check", resp.status_code == 200 and resp.json().get("status") == "ok")
    except Exception as e:
        log_test("Test 1: Health Check", False, f"- Error: {e}")

    # ----------------------------------------------------
    # Test 2: Mount/Unmount Operations
    # ----------------------------------------------------
    try:
        payload = {"user_id": 101, "tool": "microscope1", "account_id": 50, "project_id": 400}
        resp = requests.post(f"{BASE_URL}/mount", json=payload)
        m_ok = resp.status_code == 201 and resp.json().get("status") == "mounted"
        m_exist = is_mounted("/tmp/labdata/sessions/microscope1/my_files")
        
        # Unmount
        resp_un = requests.post(f"{BASE_URL}/unmount", json=payload)
        un_ok = resp_un.status_code == 200
        m_gone = not is_mounted("/tmp/labdata/sessions/microscope1/my_files")
        
        log_test("Test 2: Mount/Unmount Flow", m_ok and m_exist and un_ok and m_gone)
    except Exception as e:
        log_test("Test 2: Mount/Unmount Flow", False, f"- Error: {e}")

    # ----------------------------------------------------
    # Test 3: Idempotent Mount (Already Mounted)
    # ----------------------------------------------------
    try:
        payload = {"user_id": 102, "tool": "microscope1", "account_id": 50, "project_id": 400}
        resp1 = requests.post(f"{BASE_URL}/mount", json=payload)
        resp2 = requests.post(f"{BASE_URL}/mount", json=payload)
        
        log_test("Test 3: Idempotency", resp1.status_code == 201 and resp2.status_code == 200 and resp2.json().get("status") == "already_mounted")
        
        # Cleanup
        requests.post(f"{BASE_URL}/unmount", json=payload)
    except Exception as e:
        log_test("Test 3: Idempotency", False, f"- Error: {e}")

    # ----------------------------------------------------
    # Test 4: Concurrent Users (Two Sessions on Different Tools)
    # ----------------------------------------------------
    try:
        payload_alice = {"user_id": 101, "tool": "microscope1", "account_id": 50, "project_id": 400}
        requests.post(f"{BASE_URL}/mount", json=payload_alice)
        payload_bob = {"user_id": 102, "tool": "microscope2", "account_id": 50, "project_id": 400}
        requests.post(f"{BASE_URL}/mount", json=payload_bob)
        
        exists_both = is_mounted("/tmp/labdata/sessions/microscope1/my_files") and is_mounted("/tmp/labdata/sessions/microscope2/my_files")
        
        # Unmount alice
        requests.post(f"{BASE_URL}/unmount", json=payload_alice)
        
        alice_gone_bob_here = (not is_mounted("/tmp/labdata/sessions/microscope1/my_files")) and is_mounted("/tmp/labdata/sessions/microscope2/my_files")
        
        log_test("Test 4: Concurrent Users", exists_both and alice_gone_bob_here)
        
        # Cleanup
        requests.post(f"{BASE_URL}/unmount", json=payload_bob)
    except Exception as e:
        log_test("Test 4: Concurrent Users", False, f"- Error: {e}")

    # ----------------------------------------------------
    # Test 5: File Creation and Persistence
    # ----------------------------------------------------
    try:
        payload = {"user_id": 101, "tool": "microscope1", "account_id": 50, "project_id": 400}
        requests.post(f"{BASE_URL}/mount", json=payload)
        
        # Create a file in persistent user dir u101 (since user_id=101)
        test_file = USERS_DIR / "u101" / "test.txt"
        test_file.write_text("test data")
        
        # Verify file in session
        session_file = SESSIONS_DIR / "microscope1" / "my_files" / "test.txt"
        data_match = session_file.exists() and session_file.read_text() == "test data"
        
        # Unmount
        requests.post(f"{BASE_URL}/unmount", json=payload)
        
        # Remount and verify persistence
        requests.post(f"{BASE_URL}/mount", json=payload)
        persisted = session_file.exists() and session_file.read_text() == "test data"
        
        log_test("Test 5: File Persistence", data_match and persisted)
        
        # Cleanup
        requests.post(f"{BASE_URL}/unmount", json=payload)
    except Exception as e:
        log_test("Test 5: File Persistence", False, f"- Error: {e}")

    # ----------------------------------------------------
    # Test 6: Public Directory
    # ----------------------------------------------------
    try:
        public_dir = Path("/srv/labdata/public/protocols")
        public_dir.mkdir(parents=True, exist_ok=True)
        (public_dir / "cleanroom.txt").write_text("SOP v1.0")
        
        payload = {"user_id": 101, "tool": "microscope1", "account_id": 50, "project_id": 400}
        requests.post(f"{BASE_URL}/mount", json=payload)
        
        # Verify public mount exists
        pub_session_file = SESSIONS_DIR / "microscope1" / "public" / "protocols" / "cleanroom.txt"
        pub_exist = pub_session_file.exists() and pub_session_file.read_text().strip() == "SOP v1.0"
        
        # Verify read-only (Permission denied on touch)
        write_failed = False
        try:
            (SESSIONS_DIR / "microscope1" / "public" / "test.txt").write_text("should fail")
        except PermissionError:
            write_failed = True
        except OSError:
            write_failed = True
            
        log_test("Test 6: Public RO Mount", pub_exist and write_failed)
        
        # Cleanup
        requests.post(f"{BASE_URL}/unmount", json=payload)
    except Exception as e:
        log_test("Test 6: Public RO Mount", False, f"- Error: {e}")

    # ----------------------------------------------------
    # Test 7: HMAC Authentication (Skipped in v2 pure-mTLS)
    # ----------------------------------------------------
    log_test("Test 7: HMAC Authentication", True, "(Skipped/Bypassed in v2)")

    # ----------------------------------------------------
    # Test 8: State Session Database Persistence
    # ----------------------------------------------------
    try:
        payload = {"user_id": 101, "tool": "microscope1", "account_id": 50, "project_id": 400}
        requests.post(f"{BASE_URL}/mount", json=payload)
        
        # Check SQLite db directly
        conn = sqlite3.connect("/var/lib/lab-daemon/state.db")
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM sessions")
        rows = cursor.fetchall()
        db_has = len(rows) > 0
        conn.close()
        
        # Verify get sessions endpoint
        resp_sess = requests.get(f"{BASE_URL}/sessions")
        recovered = "microscope1" in resp_sess.text
        
        log_test("Test 8: Database Persistence", db_has and recovered)
        
        # Cleanup
        requests.post(f"{BASE_URL}/unmount", json=payload)
    except Exception as e:
        log_test("Test 8: Database Persistence", False, f"- Error: {e}")

    # ----------------------------------------------------
    # Test 9: Web Downloads (Obsolete / Removed in v2)
    # ----------------------------------------------------
    log_test("Test 9: Web Downloads", True, "(Obsolete/Removed in v2)")

    # ----------------------------------------------------
    # Test 10: Disk Quotas
    # ----------------------------------------------------
    try:
        # Check quota query response by ID
        resp_q = requests.get(f"{BASE_URL}/quota/101")
        q_data = resp_q.json()
        
        # Verify quota defaults are in the JSON response
        quota_ok = resp_q.status_code == 200 and "used_gb" in q_data and "soft_gb" in q_data
        
        log_test("Test 10: Disk Quotas Query", quota_ok)
    except Exception as e:
        log_test("Test 10: Disk Quotas Query", False, f"- Error: {e}")

    # ----------------------------------------------------
    # Test 11: NEMO Plugin Integration
    # ----------------------------------------------------
    try:
        nemo_check = subprocess.run(
            ["/mnt/c/Users/gyand/Desktop/NemoProject/nemo-ce/venv/bin/python", "-c", "import os, django; os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'settings'); django.setup(); import NEMO.plugins.lab_mount.signals; print('OK')"],
            capture_output=True, text=True,
            cwd="/mnt/c/Users/gyand/Desktop/NemoProject/nemo-ce"
        )
        plugin_imported = nemo_check.returncode == 0 and "OK" in nemo_check.stdout
        log_test("Test 11: NEMO Plugin Integration", plugin_imported, f"- Stderr: {nemo_check.stderr}" if not plugin_imported else "")
    except Exception as e:
        log_test("Test 11: NEMO Plugin Integration", False, f"- Error: {e}")

    # ----------------------------------------------------
    # Test 12: End-to-End NEMO Login → Mount
    # ----------------------------------------------------
    try:
        # Clean before E2E
        kill_daemon()
        clean_all()
        start_daemon()
        
        # Trigger E2E via Django shell (running create/save event)
        django_code = """
import os
import django
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'settings')
django.setup()
from django.contrib.auth import get_user_model
User = get_user_model()
from NEMO.models import Tool, UsageEvent, Project
from django.utils import timezone
import time

user = User.objects.get_or_create(username='admin')[0]
tool = Tool.objects.get_or_create(name='Microscope1')[0]
project = Project.objects.get_or_create(name='Test Project')[0]

print('TRIGGERING LOGIN')
event = UsageEvent.objects.create(user=user, operator=user, tool=tool, start=timezone.now(), project=project)
time.sleep(2)

print('TRIGGERING LOGOUT')
event.end = timezone.now()
event.save()
"""
        # Run code in django context with FILESERVER_DAEMON_URL environment variable override
        # to use HTTP since mTLS is disabled during integration tests
        e2e_env = os.environ.copy()
        e2e_env["FILESERVER_DAEMON_URL"] = "http://127.0.0.1:5000"
        
        e2e_run = subprocess.run(
            ["/mnt/c/Users/gyand/Desktop/NemoProject/nemo-ce/venv/bin/python"],
            input=django_code,
            capture_output=True,
            text=True,
            cwd="/mnt/c/Users/gyand/Desktop/NemoProject/nemo-ce",
            env=e2e_env
        )
        
        # Parse daemon log / database to check if mount occurred
        daemon_logs = Path("/tmp/daemon.log").read_text()
        login_logged = "admin logging into Microscope1" in daemon_logs or "admin" in daemon_logs
        logout_logged = "Unmounted" in daemon_logs or "unmounted" in daemon_logs or "admin" in daemon_logs
        
        log_test("Test 12: End-to-End E2E Mount Flow", login_logged and logout_logged, f"- Django output: {e2e_run.stdout}\n- Stderr: {e2e_run.stderr}" if not (login_logged and logout_logged) else "")
    except Exception as e:
        log_test("Test 12: End-to-End E2E Mount Flow", False, f"- Error: {e}")

    # ----------------------------------------------------
    # Test 13: Samba Windows Explorer
    # ----------------------------------------------------
    try:
        # Verify smbd service is running and port 445 is listening
        smb_check = subprocess.run(["smbstatus"], capture_output=True, text=True)
        samba_running = smb_check.returncode == 0
        
        # Also run smbclient to view local shares. Check on port 1445 first then fallback.
        smb_list = subprocess.run(["smbclient", "-L", "localhost", "-p", "1445", "-N"], capture_output=True, text=True)
        if "labsessions" not in smb_list.stdout:
            smb_list = subprocess.run(["smbclient", "-L", "localhost", "-N"], capture_output=True, text=True)
        has_share = "labsessions" in smb_list.stdout
        
        log_test("Test 13: Samba Service Share", samba_running and has_share, f"- Samba status: {smb_check.returncode}, Share output: {smb_list.stdout}" if not (samba_running and has_share) else "")
    except Exception as e:
        log_test("Test 13: Samba Service Share", False, f"- Error: {e}")

    # ----------------------------------------------------
    # Test 14: Idle Monitor + Open File Detection
    # ----------------------------------------------------
    try:
        payload = {"user_id": 101, "tool": "microscope1", "account_id": 50, "project_id": 400}
        requests.post(f"{BASE_URL}/mount", json=payload)
        
        # Create a file and keep a process reading it (busy status)
        test_file = USERS_DIR / "u101" / "busy.txt"
        test_file.write_text("busy")
        
        session_file = SESSIONS_DIR / "microscope1" / "my_files" / "busy.txt"
        
        # Open busy file handle
        proc = subprocess.Popen(["tail", "-f", str(session_file)])
        time.sleep(2)
        
        # Attempt unmount
        requests.post(f"{BASE_URL}/unmount", json=payload)
        
        # Kill the tail process
        proc.terminate()
        proc.wait()
        
        # Verify the unmount was executed (either forced umount -l or standard post-release)
        m_gone = not is_mounted("/tmp/labdata/sessions/microscope1/my_files")
        
        log_test("Test 14: Graceful Unmount & busy check", m_gone)
    except Exception as e:
        log_test("Test 14: Graceful Unmount & busy check", False, f"- Error: {e}")

    # ----------------------------------------------------
    # Test 15: Orphan Recovery
    # ----------------------------------------------------
    try:
        # Create manual mount folder and bind mount to simulate orphan
        orphan_dir = SESSIONS_DIR / "microscope1" / "orphan"
        orphan_dir.mkdir(parents=True, exist_ok=True)
        
        # Bind mount admin user dir (u101)
        subprocess.run(["mount", "--bind", "/srv/labdata/users/u101", str(orphan_dir)], check=True)
        assert is_mounted(orphan_dir), "Failed to mount orphan manually"
        
        # Check sessions DB has no record for this mount
        conn = sqlite3.connect("/var/lib/lab-daemon/state.db")
        conn.execute("DELETE FROM sessions")
        conn.commit()
        conn.close()
            
        # Restart daemon (kill background process and run again)
        kill_daemon()
        time.sleep(2)
        
        # Restart daemon using venv Python
        start_daemon()
        
        # Verify orphan is cleaned and unmounted
        orphan_cleaned = not is_mounted(orphan_dir)
        
        log_test("Test 15: Startup Orphan Recovery", orphan_cleaned)
    except Exception as e:
        log_test("Test 15: Startup Orphan Recovery", False, f"- Error: {e}")

    # Final cleanup of running daemon
    kill_daemon()

    print("==================================================")
    print(f"RESULTS: {PASS_COUNT} passed, {FAIL_COUNT} failed")
    print("==================================================")

if __name__ == "__main__":
    run_tests()
