# Diagram B: Sequence Diagram (Tool Login/Logout)

This sequence diagram documents the end-to-end communication flow during tool activation and deactivation, highlighting the secure delegation of commands from the web-facing listener to the privileged root controller.

---

## 💡 Image Generation Prompt (For UML/Sequence Diagram Renderers)

> **Prompt:** A professional 2D UML sequence diagram mapping the check-in and check-out data flow in a laboratory file server system. Designed in a vector style with a white background. It features the following color-coded lifelines: "Researcher" (Purple), "NEMO Web App" (Blue), "lab_mount Plugin" (Blue), "Daemon Listener" (Green), "Unix Socket" (Light Gray), "Daemon Controller" (Dark Green), "Storage Mounts" (Purple), and "Tool PC" (Orange).
> 
> * **Mount Flow:** Step-by-step labels showing NEMO firing signals, the plugin initiating mTLS HTTPS connections, the Listener verifying certs and writing JSON command messages to the Unix socket `/var/run/lab-daemon.sock`, and the root Controller reading the socket, executing local OS actions (`mount --bind`, `setfacl`), and sending back status codes.
> * **Unmount Flow:** Sequence showing checkout triggers, lazy unmount (`umount -l`) after checking open files via `lsof`/`smbstatus`, and cleaning up folder targets and local SQLite states.

---

## 📊 Mermaid Diagram Code

You can copy-paste the block below into any Mermaid-compatible editor (such as [mermaid.live](https://mermaid.live)):

```mermaid
sequenceDiagram
    autonumber
    actor User as Researcher
    participant NEMO as NEMO Web App
    participant Plugin as lab_mount Plugin
    participant Listener as Daemon Listener
    participant Socket as Unix Socket
    participant Controller as Daemon Controller
    participant Store as NFS Storage Mounts
    participant PC as Tool PC

    rect rgb(240, 248, 255)
        Note over User, PC: PART 1: TOOL CHECK-IN (MOUNT EVENT)
        User->>NEMO: Check into Tool + select Project in NEMO browser
        NEMO->>Plugin: Django triggers post_save signal on UsageEvent
        Plugin->>Listener: Initiate mTLS Handshake & HTTP POST /mount
        Listener->>Listener: Verify client certificate against trusted CA
        Listener->>Listener: Parse JSON & validate request payload
        Listener->>Socket: Write mount command: {"action": "mount", "user": "u123", "tool": "microscope1", "project": "proj_456"}
        Controller->>Socket: Listen & read command from /var/run/lab-daemon.sock (root level)
        Controller->>Store: Create dynamic mount folders
        Controller->>Store: Apply ACLs: setfacl -m u:microscope1_machine:rwx
        Controller->>Store: mount --bind /srv/labdata/users/u123/ -> /tmp/labdata/sessions/microscope1/my_files/
        Controller->>Store: mount --bind /srv/labdata/groups/account_X/project_456/ -> /tmp/labdata/sessions/microscope1/my_groups/C2QA
        Controller-->>Socket: Return success status code
        Listener-->>Plugin: HTTP 201 Created (via secure mTLS HTTPS channel)
        PC->>Store: SMB connects using microscope1_machine credentials
        PC-->>User: User immediately sees files appear in my_files/ and my_groups/C2QA/
    end

    rect rgb(255, 240, 245)
        Note over User, PC: PART 2: TOOL CHECK-OUT (UNMOUNT EVENT)
        User->>NEMO: Checks out of Tool in NEMO browser
        NEMO->>Plugin: Django triggers post_save signal (end timestamp set)
        Plugin->>Listener: Initiate mTLS Handshake & HTTP POST /unmount
        Listener->>Listener: Verify client certificate & validate payload
        Listener->>Socket: Write unmount command: {"action": "unmount", "user": "u123", "tool": "microscope1"}
        Controller->>Socket: Read command from /var/run/lab-daemon.sock
        Controller->>Store: Run lsof / smbstatus to verify no files are currently open
        Note over Controller, Store: Wait up to 30s grace period if files are open
        Controller->>Store: umount -l (lazy unmount user & project directories)
        Controller->>Store: Revoke ACL access: setfacl -x u:microscope1_machine
        Controller->>Store: Clean up empty session folders
        Controller-->>Socket: Return success status code
        Listener-->>Plugin: HTTP 200 OK (via secure mTLS HTTPS channel)
        PC-->>User: Samba share is instantly empty
    end
```
