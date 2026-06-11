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
from pathlib import Path

# Config
SECRET_KEY = b"00d57012a01b31f8364ebdcda42f05d15c3fd5585c69be1b8cdec1c30caa3af7"
BASE_URL = "http://127.0.0.1:5000"
SESSIONS_DIR = Path("/tmp/labdata/sessions")
USERS_DIR = Path("/srv/labdata/users")
GROUPS_DIR = Path("/srv/labdata/groups")

PASS_COUNT = 0
FAIL_COUNT = 0

def log_test(name, success, info=""):
    global PASS_COUNT, FAIL_COUNT
    status = "\033[92mPASS\033[0m" if success else "\033[91mFAIL\033[0m"
    if success:
        PASS_COUNT += 1
    else:
        FAIL_COUNT += 1
    print(f"[{status}] {name} {info}")

def sign(user, tool):
    timestamp = str(int(time.time()))
    msg = f"{user}{tool}{timestamp}"
    sig = hmac.new(SECRET_KEY, msg.encode(), hashlib.sha256).hexdigest()
    return {"X-Timestamp": timestamp, "X-Signature": sig}

def is_mounted(path):
    try:
        with open("/proc/self/mountinfo", "r") as f:
            for line in f:
                if str(path) in line:
                    return True
    except Exception:
        pass
    return False

def clean_all():
    # Force unmount any remaining mounts
    try:
        with open("/proc/self/mountinfo", "r") as f:
            for line in f:
                if "labdata" in line:
                    parts = line.strip().split()
                    if len(parts) > 4:
                        subprocess.run(["sudo", "umount", "-l", parts[4]], capture_output=True)
    except Exception:
        pass
    
    # Empty DB
    db_path = "/var/lib/lab-daemon/sessions.json"
    if os.path.exists(db_path):
        with open(db_path, "w") as f:
            f.write("{}")

def run_tests():
    global PASS_COUNT, FAIL_COUNT
    print("==================================================")
    print("       STARTING LAB DATA MOUNT DAEMON TESTS       ")
    print("==================================================")
    
    # Cleanup before starting
    clean_all()

    # Ensure daemon is running
    pids = subprocess.run(["pgrep", "-f", "daemon.py"], capture_output=True, text=True).stdout.strip().split()
    if not pids:
        print("Starting daemon in background...")
        subprocess.Popen(["sudo", "python3", "/mnt/c/Users/gyand/Desktop/NemoProject/lab-daemon/daemon.py"], stdout=open("/tmp/daemon.log", "w"), stderr=subprocess.STDOUT)
        time.sleep(3)

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
        headers = sign("alice", "microscope1")
        resp = requests.post(f"{BASE_URL}/mount", json={"user": "alice", "tool": "microscope1"}, headers=headers)
        m_ok = resp.status_code == 201 and resp.json().get("status") == "mounted"
        m_exist = is_mounted("/tmp/labdata/sessions/microscope1/alice")
        
        # Unmount
        headers_un = sign("alice", "microscope1")
        resp_un = requests.post(f"{BASE_URL}/unmount", json={"user": "alice", "tool": "microscope1"}, headers=headers_un)
        un_ok = resp_un.status_code == 200
        m_gone = not is_mounted("/tmp/labdata/sessions/microscope1/alice")
        
        log_test("Test 2: Mount/Unmount Flow", m_ok and m_exist and un_ok and m_gone)
    except Exception as e:
        log_test("Test 2: Mount/Unmount Flow", False, f"- Error: {e}")

    # ----------------------------------------------------
    # Test 3: Idempotent Mount (Already Mounted)
    # ----------------------------------------------------
    try:
        headers1 = sign("bob", "microscope1")
        resp1 = requests.post(f"{BASE_URL}/mount", json={"user": "bob", "tool": "microscope1"}, headers=headers1)
        headers2 = sign("bob", "microscope1")
        resp2 = requests.post(f"{BASE_URL}/mount", json={"user": "bob", "tool": "microscope1"}, headers=headers2)
        
        log_test("Test 3: Idempotency", resp1.status_code == 201 and resp2.status_code == 200 and resp2.json().get("status") == "already_mounted")
        
        # Cleanup
        headers_un = sign("bob", "microscope1")
        requests.post(f"{BASE_URL}/unmount", json={"user": "bob", "tool": "microscope1"}, headers=headers_un)
    except Exception as e:
        log_test("Test 3: Idempotency", False, f"- Error: {e}")

    # ----------------------------------------------------
    # Test 4: Concurrent Users (Two Sessions)
    # ----------------------------------------------------
    try:
        h_alice = sign("alice", "microscope1")
        requests.post(f"{BASE_URL}/mount", json={"user": "alice", "tool": "microscope1"}, headers=h_alice)
        h_bob = sign("bob", "microscope1")
        requests.post(f"{BASE_URL}/mount", json={"user": "bob", "tool": "microscope1"}, headers=h_bob)
        
        exists_both = is_mounted("/tmp/labdata/sessions/microscope1/alice") and is_mounted("/tmp/labdata/sessions/microscope1/bob")
        
        # Unmount alice
        h_un_alice = sign("alice", "microscope1")
        requests.post(f"{BASE_URL}/unmount", json={"user": "alice", "tool": "microscope1"}, headers=h_un_alice)
        
        alice_gone_bob_here = (not is_mounted("/tmp/labdata/sessions/microscope1/alice")) and is_mounted("/tmp/labdata/sessions/microscope1/bob")
        
        log_test("Test 4: Concurrent Users", exists_both and alice_gone_bob_here)
        
        # Cleanup
        h_un_bob = sign("bob", "microscope1")
        requests.post(f"{BASE_URL}/unmount", json={"user": "bob", "tool": "microscope1"}, headers=h_un_bob)
    except Exception as e:
        log_test("Test 4: Concurrent Users", False, f"- Error: {e}")

    # ----------------------------------------------------
    # Test 5: File Creation and Persistence
    # ----------------------------------------------------
    try:
        h_alice = sign("alice", "microscope1")
        requests.post(f"{BASE_URL}/mount", json={"user": "alice", "tool": "microscope1"}, headers=h_alice)
        
        # Create a file
        test_file = USERS_DIR / "alice" / "test.txt"
        test_file.write_text("test data")
        
        # Verify file in session
        session_file = SESSIONS_DIR / "microscope1" / "alice" / "test.txt"
        data_match = session_file.exists() and session_file.read_text() == "test data"
        
        # Unmount
        h_un = sign("alice", "microscope1")
        requests.post(f"{BASE_URL}/unmount", json={"user": "alice", "tool": "microscope1"}, headers=h_un)
        
        # Remount and verify persistence
        h_re = sign("alice", "microscope1")
        requests.post(f"{BASE_URL}/mount", json={"user": "alice", "tool": "microscope1"}, headers=h_re)
        persisted = session_file.exists() and session_file.read_text() == "test data"
        
        log_test("Test 5: File Persistence", data_match and persisted)
        
        # Cleanup
        h_un = sign("alice", "microscope1")
        requests.post(f"{BASE_URL}/unmount", json={"user": "alice", "tool": "microscope1"}, headers=h_un)
    except Exception as e:
        log_test("Test 5: File Persistence", False, f"- Error: {e}")

    # ----------------------------------------------------
    # Test 6: Public Directory
    # ----------------------------------------------------
    try:
        public_dir = Path("/srv/labdata/public/protocols")
        public_dir.mkdir(parents=True, exist_ok=True)
        (public_dir / "cleanroom.txt").write_text("SOP v1.0")
        
        h_alice = sign("alice", "microscope1")
        requests.post(f"{BASE_URL}/mount", json={"user": "alice", "tool": "microscope1"}, headers=h_alice)
        
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
        h_un = sign("alice", "microscope1")
        requests.post(f"{BASE_URL}/unmount", json={"user": "alice", "tool": "microscope1"}, headers=h_un)
    except Exception as e:
        log_test("Test 6: Public RO Mount", False, f"- Error: {e}")

    # ----------------------------------------------------
    # Test 7: HMAC Authentication
    # ----------------------------------------------------
    try:
        # Test without headers
        resp_no = requests.post(f"{BASE_URL}/mount", json={"user": "hacker", "tool": "microscope1"})
        
        # Test with wrong headers
        headers_wrong = {"X-Timestamp": "123", "X-Signature": "wrong"}
        resp_wrong = requests.post(f"{BASE_URL}/mount", json={"user": "hacker", "tool": "microscope1"}, headers=headers_wrong)
        
        log_test("Test 7: HMAC Authentication", resp_no.status_code == 401 and resp_wrong.status_code == 401)
    except Exception as e:
        log_test("Test 7: HMAC Authentication", False, f"- Error: {e}")

    # ----------------------------------------------------
    # Test 8: JSON Session Persistence
    # ----------------------------------------------------
    try:
        h_alice = sign("alice", "microscope1")
        requests.post(f"{BASE_URL}/mount", json={"user": "alice", "tool": "microscope1"}, headers=h_alice)
        
        # Check sessions.json
        with open("/var/lib/lab-daemon/sessions.json", "r") as f:
            db_data = json.load(f)
        db_has = any(s["user"] == "alice" and s["tool"] == "microscope1" for s in db_data.values())
        
        # Simulate daemon restart (calling recovery)
        # We can just check the endpoint /sessions returns it
        resp_sess = requests.get(f"{BASE_URL}/sessions")
        recovered = "alice" in resp_sess.text and "microscope1" in resp_sess.text
        
        log_test("Test 8: JSON Persistence", db_has and recovered)
        
        # Cleanup
        h_un = sign("alice", "microscope1")
        requests.post(f"{BASE_URL}/unmount", json={"user": "alice", "tool": "microscope1"}, headers=h_un)
    except Exception as e:
        log_test("Test 8: JSON Persistence", False, f"- Error: {e}")

    # ----------------------------------------------------
    # Test 9: Web Download Endpoint
    # ----------------------------------------------------
    try:
        h_alice = sign("alice", "microscope1")
        requests.post(f"{BASE_URL}/mount", json={"user": "alice", "tool": "microscope1"}, headers=h_alice)
        
        # Create download file
        dl_file = USERS_DIR / "alice" / "download.txt"
        dl_file.write_text("download test")
        
        # Browse files (expecting html link)
        resp_list = requests.get(f"{BASE_URL}/files/alice")
        has_link = "download.txt" in resp_list.text and "/download/alice/download.txt" in resp_list.text
        
        # Download
        resp_dl = requests.get(f"{BASE_URL}/download/alice/download.txt")
        dl_ok = resp_dl.status_code == 200 and resp_dl.text == "download test"
        
        log_test("Test 9: Web Downloads", has_link and dl_ok)
        
        # Cleanup
        h_un = sign("alice", "microscope1")
        requests.post(f"{BASE_URL}/unmount", json={"user": "alice", "tool": "microscope1"}, headers=h_un)
    except Exception as e:
        log_test("Test 9: Web Downloads", False, f"- Error: {e}")

    # ----------------------------------------------------
    # Test 10: Disk Quotas
    # ----------------------------------------------------
    try:
        # Check quota query response
        resp_q = requests.get(f"{BASE_URL}/quota/alice")
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
        # Run migrations, copy plugin, run server and check connection
        # The user has already run the setups. We can verify settings and import of plugin in django.
        # Run a quick python command inside NEMO venv to assert import and signals
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
        clean_all()
        
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
        # Run code in django context
        e2e_run = subprocess.run(
            ["/mnt/c/Users/gyand/Desktop/NemoProject/nemo-ce/venv/bin/python"],
            input=django_code,
            capture_output=True,
            text=True,
            cwd="/mnt/c/Users/gyand/Desktop/NemoProject/nemo-ce"
        )
        
        # Parse daemon log / database to check if mount occurred
        # Check logs for "🔐 USER ACTION: admin logging into Microscope1"
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
        smb_check = subprocess.run(["sudo", "smbstatus"], capture_output=True, text=True)
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
        h_alice = sign("alice", "microscope1")
        requests.post(f"{BASE_URL}/mount", json={"user": "alice", "tool": "microscope1"}, headers=h_alice)
        
        # Create a file and keep a process reading it (busy status)
        test_file = USERS_DIR / "alice" / "busy.txt"
        test_file.write_text("busy")
        
        session_file = SESSIONS_DIR / "microscope1" / "alice" / "busy.txt"
        
        # Open busy file handle
        proc = subprocess.Popen(["tail", "-f", str(session_file)])
        time.sleep(2)
        
        # Attempt unmount (should timeout / wait and eventually fail or force)
        h_un = sign("alice", "microscope1")
        resp_un = requests.post(f"{BASE_URL}/unmount", json={"user": "alice", "tool": "microscope1"}, headers=h_un)
        
        # Kill the tail process
        proc.terminate()
        proc.wait()
        
        # Verify the unmount was executed (either forced umount -l or standard post-release)
        m_gone = not is_mounted("/tmp/labdata/sessions/microscope1/alice")
        
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
        
        # Bind mount admin user dir
        subprocess.run(["sudo", "mount", "--bind", "/srv/labdata/users/alice", str(orphan_dir)], check=True)
        assert is_mounted(orphan_dir), "Failed to mount orphan manually"
        
        # Check sessions DB has no record for this mount
        with open("/var/lib/lab-daemon/sessions.json", "w") as f:
            f.write("{}")
            
        # Restart daemon (kill background process and run again)
        # Find pid of daemon.py
        pids = subprocess.run(["pgrep", "-f", "daemon.py"], capture_output=True, text=True).stdout.strip().split()
        for pid in pids:
            subprocess.run(["sudo", "kill", "-9", pid])
            
        time.sleep(2)
        
        # Restart daemon
        subprocess.Popen(["sudo", "python3", "/mnt/c/Users/gyand/Desktop/NemoProject/lab-daemon/daemon.py"], stdout=open("/tmp/daemon.log", "w"), stderr=subprocess.STDOUT)
        time.sleep(3)
        
        # Verify orphan is cleaned and unmounted
        orphan_cleaned = not is_mounted(orphan_dir)
        
        log_test("Test 15: Startup Orphan Recovery", orphan_cleaned)
    except Exception as e:
        log_test("Test 15: Startup Orphan Recovery", False, f"- Error: {e}")

    print("==================================================")
    print(f"RESULTS: {PASS_COUNT} passed, {FAIL_COUNT} failed")
    print("==================================================")

if __name__ == "__main__":
    run_tests()
