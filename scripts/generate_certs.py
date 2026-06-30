import subprocess
import os
from pathlib import Path

def run_cmd(cmd):
    print(f"Running: {' '.join(cmd)}")
    res = subprocess.run(cmd, capture_output=True, text=True)
    if res.returncode != 0:
        print(f"Error: {res.stderr}")
        raise RuntimeError(f"Command failed: {res.stderr}")
    return res.stdout

def main():
    certs_dir = Path(__file__).parent.parent / "certs"
    certs_dir.mkdir(exist_ok=True)
    
    os.chdir(certs_dir)
    print(f"Generating certificates in {certs_dir.resolve()}")
    
    # 1. Generate CA key and certificate
    if not os.path.exists("ca.key"):
        run_cmd(["openssl", "genrsa", "-out", "ca.key", "4096"])
        run_cmd(["openssl", "req", "-new", "-x509", "-days", "3650", "-key", "ca.key", "-out", "ca.crt", "-subj", "/CN=LabDaemonCA"])
        print("Generated CA certificate.")
    
    # 2. Generate Server key, CSR, and certificate
    if not os.path.exists("server.key"):
        run_cmd(["openssl", "genrsa", "-out", "server.key", "2048"])
        run_cmd(["openssl", "req", "-new", "-key", "server.key", "-out", "server.csr", "-subj", "/CN=localhost"])
        run_cmd(["openssl", "x509", "-req", "-days", "365", "-in", "server.csr", "-CA", "ca.crt", "-CAkey", "ca.key", "-CAcreateserial", "-out", "server.crt"])
        print("Generated Server certificate.")
        
    # 3. Generate Client key, CSR, and certificate
    if not os.path.exists("client.key"):
        run_cmd(["openssl", "genrsa", "-out", "client.key", "2048"])
        run_cmd(["openssl", "req", "-new", "-key", "client.key", "-out", "client.csr", "-subj", "/CN=nemo-client"])
        run_cmd(["openssl", "x509", "-req", "-days", "365", "-in", "client.csr", "-CA", "ca.crt", "-CAkey", "ca.key", "-CAcreateserial", "-out", "client.crt"])
        print("Generated Client certificate.")
        
    print("Certificate generation complete!")

if __name__ == "__main__":
    main()
