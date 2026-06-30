# Diagram C: User Authentication Flow (NFC & Samba Mappings)

This diagram outlines how human researchers and air-gapped lab computers authenticate. The human authentication flow for both Princeton NetIDs and Guest Accounts has been merged into a single, unified Nextcloud SAML SSO swimlane.

---

## 💡 Image Generation Prompt (For Swimlane / DFD Renderers)

> **Prompt:** A professional 2D flow diagram containing two horizontal swimlane rows: "Lane 1: Web Portal Access (Nextcloud - Human Researchers)" (Blue) and "Lane 2: Instrument Access (Samba Share - Tool PCs)" (Orange). White background.
> 
> * **Lane 1 (Web Portal Access - Blue):** Shows the path for both Princeton NetIDs and external guest accounts.
>   * *Steps:* Researcher opens Nextcloud browser $\to$ Redirects to Princeton Shibboleth IdP $\to$ User logs in (NetID or Guest credential) $\to$ Shibboleth issues SAML Assertion mapping `uid` to Unix user `u{user_id}` $\to$ Nextcloud maps user home workspace to local path `/srv/labdata/users/u{user_id}` $\to$ Nextcloud queries Linux directory `/srv/labdata/groups/` and filters visible projects based on the user's permanent Linux group memberships (`proj_{id}`) $\to$ Researcher reads/writes files on personal laptop.
> * **Lane 2 (Instrument Access - Orange):** Shows the path for physical lab computers.
>   * *Steps:* Tool PC boots up $\to$ Auto-connects to local Samba share `\\fileserver\labsessions\tool` $\to$ Credentials stored in Windows Credential Manager are passed (`tool_machine`) $\to$ Samba server verifies credentials locally via `passdb.tdb` (No AD/SSO/SAML involved) $\to$ Tool PC reads/writes to mounted session folders only when a session is active (controlled by the daemon).
> * **Legend & Comparison Table:** At the bottom, show a clean comparison matrix of the two lanes (Protocol, User types, Auth check, Security boundary).

---

## 📊 Mermaid Diagram Code

You can copy-paste the block below into any Mermaid-compatible editor (such as [mermaid.live](https://mermaid.live)):

```mermaid
graph TD
    %% Lane 1: Nextcloud / SAML Web Portal for Humans
    subgraph Lane_1 [Lane 1: Web Portal Access — Nextcloud SAML SSO (NetIDs & Guest Accounts)]
        L1_1[1. User opens nextcloud.princeton.edu in browser]
        L1_2[2. Nextcloud redirects to Princeton Shibboleth IdP]
        L1_3[3. User enters credentials <br> NetID or Guest Password]
        L1_4[4. Shibboleth validates credentials]
        L1_5[5. Shibboleth returns SAML Assertion mapping uid to u_id]
        L1_6[6. Nextcloud logs user in as u_id]
        L1_7[7. Nextcloud maps myFiles to /srv/labdata/users/u_id/]
        L1_8[8. Nextcloud maps myGroups to /srv/labdata/groups/]
        L1_9[9. Linux OS checks ACLs and groups in real-time]
        L1_10[10. User reads/writes files permanently based on OS groups]

        L1_1 --> L1_2 --> L1_3 --> L1_4 --> L1_5 --> L1_6 --> L1_7 & L1_8 --> L1_9 --> L1_10
    end

    %% Lane 2: Samba Share for Tool PCs
    subgraph Lane_2 [Lane 2: Instrument Access — Samba Share (Air-Gapped Tool PCs)]
        L2_1[1. Tool PC boots up in secure lab VLAN]
        L2_2[2. Windows auto-logs in to local OS account]
        L2_3[3. Samba client auto-connects to fileserver]
        L2_4[4. Credentials retrieved: tool_machine & password]
        L2_5[5. Fileserver Samba validates locally via passdb.tdb]
        L2_6[6. Samba share mounts successfully but appears empty]
        L2_7[7. Check-in: Daemon bind-mounts folders to sessions/tool/]
        L2_8[8. Tool PC writes microscope data to network share]
        L2_9[9. Check-out: Daemon unmounts directories and share is empty again]

        L2_1 --> L2_2 --> L2_3 --> L2_4 --> L2_5 --> L2_6 --> L2_7 --> L2_8 --> L2_9
        L2_9 --> L2_3
    end

    %% Styling
    classDef blue fill:#e6f2ff,stroke:#004080,stroke-width:1px;
    classDef orange fill:#fff5e6,stroke:#804000,stroke-width:1px;

    class L1_1,L1_2,L1_3,L1_4,L1_5,L1_6,L1_7,L1_8,L1_9,L1_10 blue;
    class L2_1,L2_2,L2_3,L2_4,L2_5,L2_6,L2_7,L2_8,L2_9 orange;
```

---

## 📋 Mappings & Authentication Matrix

| Category | Lane 1: Human Web Access (Laptops) | Lane 2: Machine Access (Tool PCs) |
| :--- | :--- | :--- |
| **User Scope** | All Princeton NetID users AND Guest external users. | Under 10 physical instrument computers. |
| **Access Client** | Nextcloud Client or Web Browser (Port 443). | Native Windows Explorer / Samba (Port 445). |
| **Authentication** | SAML SSO (Princeton Shibboleth IdP). | Local Samba User DB on VM (`passdb.tdb`). |
| **Dynamic Mounts** | **None.** Folders map permanently. OS-level group policies hide unauthorized paths. | **Yes.** Folders are mounted at check-in and unmounted at check-out. |
| **VPN Required** | No. Nextcloud is exposed securely via Port 443. | Yes. Virtual LAN connection / SSH Tunnel required. |
