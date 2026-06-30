# Production Deployment Guide: Lab Data Mount System

This document outlines the production installation, configuration, and security architecture for deploying the Princeton MNFC Lab Data Mount System. 

---

## 🏗️ 1. Architecture Overview (The "Flexible Box" Paradigm)

To ensure this system is future-proof and reusable by other universities, the deployment adheres to the **"Flexible Box"** concept:
1. **OS-Level Permissions as Source of Truth:** All access rights are managed directly inside the Linux filesystem using standard POSIX ACLs (`setfacl`) and Unix groups (`proj_{project_id}`). No proprietary database is used to track permissions.
2. **Interchangeable Frontend Portal:** Nextcloud serves as the web portal and authenticates users via Princeton's SAML (Shibboleth) SSO. Because Nextcloud reads permissions directly from the OS/Samba layout, Nextcloud is completely hot-swappable. Other institutions can replace it with another portal or connect directly via Samba and Active Directory without rewriting any daemon code.
3. **Decoupled Daemon Security:** To implement the principle of least privilege, the Lab Daemon is split into two components:
   * **Daemon Listener (Unprivileged):** A Flask web application running under a low-privilege system account (e.g., `www-data`). It handles mTLS handshakes, validates payloads, and writes actions to a secure local Unix socket.
   * **Daemon Controller (Privileged):** A backend worker script running as `root` locally on the server. It listens *only* to the local Unix socket (completely blocked from network traffic) and executes low-level system commands (`mount`, `umount`, `setfacl`, `useradd`).

---

## 📦 2. Prerequisites & Server Packages

Send this list of packages to the Princeton Server Team to install on the deployment server (Ubuntu/Debian environment):

```bash
# 1. Storage & OS Utilities
sudo apt install -y acl cifs-utils lsof sqlite3

# 2. Python 3 Environment (for Lab Daemon)
sudo apt install -y python3 python3-pip python3-venv python3-dev

# 3. Nginx Proxy & Samba File Server
sudo apt install -y nginx openssl samba samba-common-bin

# 4. Nextcloud Application Stack (PHP-FPM, Database, Caching)
sudo apt install -y mariadb-server php-fpm php-mysql php-common php-curl \
                    php-gd php-xml php-mbstring php-zip php-intl \
                    php-apcu php-bcmath php-gmp php-imagick

# 5. Redis (Required for Nextcloud high-performance file locking)
sudo apt install -y redis-server php-redis
```

### 🛠️ Required Packages & Utilities

*   **acl**: Linux Access Control Lists utility. Essential for setting dynamic user permissions (`setfacl`) on folders.
*   **cifs-utils**: Filesystem mount utility. Required to mount the remote 6 TB storage pool onto the VM.
*   **lsof**: List Open Files utility. The daemon runs this before unmounting to prevent data corruption.
*   **sqlite3**: Local lightweight database. Used by the daemon to keep track of active sessions.
*   **python3, python3-pip, python3-venv, python3-dev**: Core Python 3 development runtime and tools used to run the Lab Daemon.
*   **nginx**: Web server and proxy. Used to terminate secure mTLS client certificates from NEMO and serve Nextcloud.
*   **openssl**: SSL/TLS library. Used to generate and manage certificates.
*   **samba, samba-common-bin**: File sharing server. Shares active session folders to lab instrument computers.
*   **mariadb-server**: Relational database. Acts as the primary backend database for Nextcloud.
*   **php-fpm (and modules)**: The PHP fast process manager and dependencies required to execute the Nextcloud web app.
*   **redis-server, php-redis**: In-memory caching server. Speeds up Nextcloud sessions and prevents file-lock database collisions.

---


## 💾 3. Storage Mount Configuration (6 TB SMB Share)

The 6 TB primary storage directory resides on a separate server and is mounted as an SMB share.

> [!IMPORTANT]
> Because standard Linux quotas (`setquota`) do not function on mounted network SMB shares, you must coordinate with the storage provider to handle user/group size limits at the host level, or configure the daemon to compute folder usage dynamically via `du` scans.

To support the daemon's permission updates, the SMB share must be mounted on the VM with **POSIX ACL support enabled**. 

Add the following to `/etc/fstab` on the server:
```text
//storage_server/labdata /srv/labdata cifs credentials=/etc/samba/.storage_credentials,iocharset=utf8,nounix,cifsacl,file_mode=0770,dir_mode=0770 0 0
```
Create `/etc/samba/.storage_credentials` containing:
```text
username=your_storage_user
password=your_storage_password
domain=princeton.edu
```
Secure the credentials file:
```bash
sudo chmod 600 /etc/samba/.storage_credentials
```

---

## 🔒 4. Nginx mTLS & Reverse Proxy Configuration

Nginx acts as the secure web gateway. It terminates client certificate validation (mTLS) for NEMO calls and routes regular HTTP traffic to Nextcloud.

Create `/etc/nginx/sites-available/nemo-gateway`:
```nginx
# 1. NEMO-CE Control Channel (mTLS Port 5000)
server {
    listen 5000 ssl;
    server_name fileserver.princeton.edu;

    # Server Certificates
    ssl_certificate /etc/ssl/certs/lab-daemon.crt;
    ssl_certificate_key /etc/ssl/private/lab-daemon.key;

    # Client Certificate Verification (mTLS)
    ssl_client_certificate /etc/ssl/certs/lab-daemon-ca.crt;
    ssl_verify_client on;
    ssl_protocols TLSv1.2 TLSv1.3;

    location / {
        proxy_pass http://127.0.0.1:8080;  # Forward to Daemon Listener
        proxy_set_header X-Client-DN $ssl_client_s_dn;
        proxy_set_header X-Client-Verify $ssl_client_verify;
    }
}

# 2. Nextcloud Web Portal (HTTPS Port 443)
server {
    listen 443 ssl http2;
    server_name nextcloud.princeton.edu;

    ssl_certificate /etc/ssl/certs/nextcloud.crt;
    ssl_certificate_key /etc/ssl/private/nextcloud.key;

    # Nextcloud security headers & rules
    add_header Referrer-Policy "no-referrer" always;
    add_header X-Content-Type-Options "nosniff" always;
    add_header X-Frame-Options "SAMEORIGIN" always;

    root /var/www/nextcloud/;
    index index.php index.html;

    location / {
        rewrite ^ /index.php$request_uri;
    }

    location ~ [^/]\.php(/|$) {
        fastcgi_split_path_info ^(.+?\.php)(/.*)$;
        include fastcgi_params;
        fastcgi_param SCRIPT_FILENAME $document_root$fastcgi_script_name;
        fastcgi_param PATH_INFO $fastcgi_path_info;
        fastcgi_pass unix:/run/php/php8.2-fpm.sock;  # Adjust PHP version as needed
    }
}
```

---

## ⚙️ 5. Lab Daemon Configuration

Edit the system configuration file on the server at `/etc/lab-daemon/config.yaml`:

```yaml
storage:
  base_path: "/srv/labdata"
  users_path: "/srv/labdata/users"
  groups_path: "/srv/labdata/groups"
  sessions_path: "/tmp/labdata/sessions"
  public_path: "/srv/labdata/public"
  
  # Flat directories layout: "project" exposes a flat list of projects
  # in my_groups. "account" exposes a flat list of accounts/PI folders.
  group_folder_type: "project" 

mtls:
  enabled: true
  ca_cert: "/etc/ssl/certs/lab-daemon-ca.crt"
  server_cert: "/etc/ssl/certs/lab-daemon.crt"
  server_key: "/etc/ssl/private/lab-daemon.key"

session:
  db_path: "/var/lib/lab-daemon/state.db"
  idle_timeout_minutes: 60
  unmount_grace_seconds: 30

sync:
  on_deactivation: "lock_account"
  dry_run: false
```

---

## ☁️ 6. Nextcloud & SAML SSO Configuration

To integrate Nextcloud with Princeton’s Web SSO (Shibboleth):

1. **Install the SAML Backend App:**
   Log into Nextcloud as an Administrator, go to **Apps**, search for `User SAML Backend` (also known as `user_saml`), and enable it.
2. **Configure SAML Settings:**
   Navigate to **Settings** -> **SSO & SAML Integration** and enter:
   * **General:** Select *Use SAML 2.0 Identity Provider*
   * **IdP Metadata URL:** `https://idp.princeton.edu/shibboleth`
   * **Attribute Mapping:** Map the Shibboleth `uid` attribute to the Nextcloud `userid` (which matches the Unix username `u{user_id}`).
3. **Mount Storage via External Storage App:**
   Enable the `External Storage Support` app in Nextcloud. Configure it to map local filesystem paths to Nextcloud directories:
   * **`myFiles`:** Map Local path `/srv/labdata/users/$user` (Nextcloud dynamically interpolates `$user` to match the logged-in SAML username).
   * **`myGroups`:** Map Local path `/srv/labdata/groups/`. Because Nextcloud runs as the `www-data` system account, it will automatically respect OS-level POSIX ACLs, only showing users the specific folders they are authorized to open.
   * **`public`:** Map Local path `/srv/labdata/public` (read-only for normal users, read-write for manager/staff).

---

## 🖥️ 7. Samba Configuration (Tool PCs)

Edit `/etc/samba/smb.conf` to expose the active session mount folders to the air-gapped lab computers:

```ini
[global]
    workgroup = WORKGROUP
    server string = MNFC Fileserver
    security = user
    map to guest = Bad User
    log file = /var/log/samba/log.%m
    max log size = 1000

[labsessions]
    comment = Active Instrument Lab Sessions
    path = /tmp/labdata/sessions
    browseable = yes
    read only = no
    guest ok = no
    create mask = 0770
    directory mask = 0770
    # Operations execute as root to resolve mounts, but SMB verifies credentials first
    force user = root
```

### Adding a new Tool PC Account
Each instrument computer connects to the file server using a unique local machine account:
1. Create the system account:
   ```bash
   sudo useradd -M -g nogroup -s /usr/sbin/nologin -c "NEMO Machine Microscope1" microscope1_machine
   ```
2. Add the user to the Samba password registry:
   ```bash
   sudo smbpasswd -a microscope1_machine
   ```
3. Map the path `\\<fileserver_ip>\labsessions\microscope1` permanently on the microscope computer using the `microscope1_machine` credentials.

---

## 🛡️ 8. Permissions & Quota Administration

The daemon manages permissions dynamically at runtime:

* **Session Activation (Mount):**
  When a user checks into a tool, the daemon runs the following to grant access to the tool's machine account:
  ```bash
  # Grant user folder access
  setfacl -m u:{tool_name}_machine:rwx /srv/labdata/users/u{user_id}
  # Grant project folder access
  setfacl -m u:{tool_name}_machine:rwx /srv/labdata/groups/account_{id}/project_{id}
  ```
* **Quota Allocation:**
  The user sync cron job allocates disk limits using:
  ```bash
  # Allocates 10GB soft limit, 12GB hard limit to Linux user
  setquota -u u{user_id} 10G 12G 0 0 /srv/labdata
  # Allocates group limits to project folder group
  setquota -g proj_{project_id} 50G 60G 0 0 /srv/labdata
  ```
