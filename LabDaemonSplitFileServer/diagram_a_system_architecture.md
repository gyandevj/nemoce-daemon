# Diagram A: Split-Server System Architecture & Data Flow

This document details the re-engineered system architecture separating the web-facing application VM, the privileged daemon controller, and the remote network storage pool.

---

## 💡 Image Generation Prompt (For Midjourney / Imagen / DALL-E)

> **Prompt:** A professional, clean 2D system architecture diagram for a secure university lab fileserver system. Designed in a clean vector style with a white background. The diagram contains four distinct color-coded regions: "NEMO Web Server VM" (Light Blue), "Storage VM Gateway" (Light Green), "Remote Storage Pool NAS" (Light Purple), and "VLAN Air-Gapped Network" (Orange).
> 
> * **NEMO Web Server VM (Light Blue):** Contains "NEMO-CE (Django Web App)" and the "lab_mount Plugin". An arrow points to the Storage VM labeled "mTLS HTTPS requests (Port 5000)".
> * **Storage VM Gateway (Light Green):** Show three compartments:
>   1. **Nginx Reverse Proxy:** Receives incoming Port 5000 requests. Verifies client TLS certificates against trusted CA.
>   2. **Daemon Listener (Unprivileged):** Running as `www-data` on Port 8080. Connected to a local SQLite `state.db`. Writes to a Unix Socket `/var/run/lab-daemon.sock`.
>   3. **Daemon Controller (Privileged):** Running as `root`. Reads from the Unix Socket and performs system operations (`mount`, `setfacl`).
>   * Also shows "Nextcloud Web Portal (Port 443)" and "Samba Server (Port 445)" running on this VM.
> * **Remote Storage Pool NAS (Light Purple):** Represents the remote 6 TB storage array. An NFS line with mount options `no_root_squash, acl` connects this storage to the Storage VM at `/srv/labdata`.
> * **VLAN Air-Gapped Network (Orange):** Shows "Tool PC (microscope1)" connecting to the Storage VM via Samba Port 445.
> * **Flows & Icons:** Clear directional vector arrows, network port labels, and simple security lock icons for mTLS.

---

## 📊 Mermaid Diagram Code

You can copy-paste the block below into any Mermaid-compatible editor (such as [mermaid.live](https://mermaid.live)):

```mermaid
graph TD
    %% Subgraph 1: NEMO Web VM
    subgraph NEMO_VM [NEMO Web App VM]
        NEMO[NEMO-CE <br> Django Web App]
        Plugin[lab_mount Plugin]
        NEMO --- Plugin
    end

    %% Subgraph 2: Storage VM Gateway
    subgraph Storage_VM [Storage VM Gateway]
        Nginx[Nginx Reverse Proxy <br> Port 5000]
        Listener[Daemon Listener <br> Flask Port 8080 <br> Runs as www-data]
        Socket(Unix Socket <br> /var/run/lab-daemon.sock)
        Controller[Daemon Controller <br> Runs as root]
        Nextcloud[Nextcloud Web Portal <br> Port 443]
        SMB[Samba smbd <br> Port 445]
        DB[(SQLite DB <br> state.db)]
        
        Nginx -->|Proxy Pass| Listener
        Listener -->|Query/Update| DB
        Listener -->|Write JSON commands| Socket
        Socket -->|Read commands| Controller
    end

    %% Subgraph 3: Remote NAS
    subgraph Remote_NAS [Remote Storage Pool NAS]
        StoragePool[(6 TB Storage Pool)]
    end

    %% Subgraph 4: VLAN
    subgraph VLAN [VLAN Air-Gapped Network]
        Tool[Tool PC <br> microscope1]
    end

    %% Subgraph 5: Princeton Network
    subgraph Princeton [Princeton Network]
        Shibboleth[Shibboleth IdP <br> NetIDs & Guests]
    end

    %% Connective Flows
    Plugin -->|"(1) mTLS HTTPS Request (Port 5000)"| Nginx
    StoragePool -->|"(2) NFS Mount (no_root_squash, acl)"| Storage_VM
    Controller -->|"(3) Bind Mounts & setfacl"| Storage_VM
    Tool -->|"(4) SMB Mount (microscope1_machine)"| SMB
    Shibboleth -->|"(5) SAML Auth assertions"| Nextcloud
    Shibboleth -->|"(5) SAML Auth assertions"| NEMO

    %% Styling
    classDef blue fill:#e6f2ff,stroke:#004080,stroke-width:1.5px;
    classDef green fill:#e6ffe6,stroke:#004000,stroke-width:1.5px;
    classDef purple fill:#f9f2ff,stroke:#400080,stroke-width:1.5px;
    classDef orange fill:#fff5e6,stroke:#804000,stroke-width:1.5px;

    class NEMO,Plugin blue;
    class Nginx,Listener,Socket,Controller,Nextcloud,SMB,DB green;
    class StoragePool purple;
    class Tool orange;
    class Shibboleth blue;
```
