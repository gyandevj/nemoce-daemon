#!/usr/bin/env python3
import os
import sys
import subprocess
import time
import shutil

# Ensure running as root
if os.geteuid() != 0:
    print("\033[1;31m[ERROR] This script must be run as root (sudo python3 storage_demo.py)\033[0m")
    sys.exit(1)

# Paths
NFS_STORAGE = "/tmp/nfs_storage"
NFS_CLIENT = "/tmp/nfs_client"
LOOP_DISK = "/tmp/loop_disk.img"
LOOP_CLIENT = "/tmp/loop_client"
EXPORTS_FILE = "/etc/exports"
EXPORTS_BAK = "/etc/exports.bak"

def print_header(title):
    print(f"\n\033[1;34m=== {title} ===\033[0m")

def run(cmd):
    # Print command in yellow (like a real shell prompt)
    print(f"\033[1;33m$ {cmd}\033[0m")
    res = subprocess.run(cmd, shell=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if res.stdout:
        print(res.stdout.strip())
    if res.stderr:
        # Print error in red
        print(f"\033[1;31m{res.stderr.strip()}\033[0m")
    return res

def cleanup():
    print_header("CLEANUP")
    # Unmount NFS
    if os.path.ismount(NFS_CLIENT):
        run(f"umount -l {NFS_CLIENT}")
    # Unmount Loop
    if os.path.ismount(LOOP_CLIENT):
        run(f"umount -l {LOOP_CLIENT}")
    
    # Detach loop devices
    res = run("losetup -j " + LOOP_DISK)
    if res.stdout:
        for line in res.stdout.splitlines():
            dev = line.split(":")[0]
            run(f"losetup -d {dev}")

    # Restore /etc/exports
    if os.path.exists(EXPORTS_BAK):
        shutil.move(EXPORTS_BAK, EXPORTS_FILE)
        run("exportfs -arv")
    
    # Delete temp files
    for path in [NFS_STORAGE, NFS_CLIENT, LOOP_DISK, LOOP_CLIENT]:
        if os.path.exists(path):
            if os.path.isdir(path):
                shutil.rmtree(path)
            else:
                os.remove(path)

# Back up /etc/exports
if os.path.exists(EXPORTS_FILE):
    shutil.copy2(EXPORTS_FILE, EXPORTS_BAK)
else:
    with open(EXPORTS_BAK, 'w') as f:
        pass

try:
    print_header("PRE-FLIGHT CHECK")
    run("apt-get update && apt-get install -y nfs-kernel-server nfs-common acl")

    # =========================================================================
    # PHASE 1: NFS with root_squash
    # =========================================================================
    print_header("PHASE 1: NFS with root_squash (Expected: Failures)")
    os.makedirs(NFS_STORAGE, exist_ok=True)
    os.makedirs(NFS_CLIENT, exist_ok=True)
    os.chmod(NFS_STORAGE, 0o777)

    # Configure root_squash export
    with open(EXPORTS_FILE, "w") as f:
        f.write(f"{NFS_STORAGE} 127.0.0.1(rw,sync,root_squash,no_subtree_check)\n")
    
    run("service nfs-kernel-server restart")
    time.sleep(1)
    
    # Mount using NFSv3 and acl options
    run(f"mount -t nfs -o nfsvers=3,acl 127.0.0.1:{NFS_STORAGE} {NFS_CLIENT}")

    # Test commands
    test_file = os.path.join(NFS_CLIENT, "nfs_test_file")
    run(f"touch {test_file}")
    run(f"chown 1000:1000 {test_file}")
    run(f"setfacl -m u:1000:rwx {test_file}")

    # =========================================================================
    # PHASE 2: NFS with no_root_squash
    # =========================================================================
    print_header("PHASE 2: NFS with no_root_squash (Expected: Success)")
    run(f"umount -l {NFS_CLIENT}")
    
    # Configure no_root_squash export
    with open(EXPORTS_FILE, "w") as f:
        f.write(f"{NFS_STORAGE} 127.0.0.1(rw,sync,no_root_squash,no_subtree_check)\n")
    
    run("exportfs -arv")
    run(f"mount -t nfs -o nfsvers=3,acl 127.0.0.1:{NFS_STORAGE} {NFS_CLIENT}")

    # Test commands
    run(f"chown 1000:1000 {test_file}")
    run(f"setfacl -m u:1000:rwx {test_file}")
    run(f"getfacl {test_file}")

    # =========================================================================
    # PHASE 3: Block Storage via Loop Device (iSCSI Equivalent)
    # =========================================================================
    print_header("PHASE 3: Block Storage via Loop Device (Expected: Success)")
    os.makedirs(LOOP_CLIENT, exist_ok=True)

    # Create raw disk file
    run(f"dd if=/dev/zero of={LOOP_DISK} bs=1M count=100")
    
    # Find free loop device and bind
    res = run("losetup -f")
    loop_dev = res.stdout.strip()
    run(f"losetup {loop_dev} {LOOP_DISK}")

    # Format natively as ext4 and mount
    run(f"mkfs.ext4 -F {loop_dev}")
    run(f"mount {loop_dev} {LOOP_CLIENT}")

    # Test commands
    loop_test = os.path.join(LOOP_CLIENT, "loop_test_file")
    run(f"touch {loop_test}")
    run(f"chown 1000:1000 {loop_test}")
    run(f"setfacl -m u:1000:rwx {loop_test}")
    run(f"getfacl {loop_test}")

finally:
    cleanup()
