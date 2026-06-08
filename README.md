# Lab Data Mount Daemon

A lightweight Flask daemon that manages bind mounts for NEMO-CE lab instrument sessions. It exposes user data directories into per-tool session paths, suitable for Samba sharing.

## Architecture

```
/tmp/labdata/
├── users/              ← Permanent per-user data
│   └── testuser/
└── sessions/           ← Mount points (exposed to Samba)
    └── microscope1/
        └── testuser/   ← Bind mount of /tmp/labdata/users/testuser
```

When a user starts a session on a tool (e.g. `microscope1`), the daemon bind-mounts their personal data directory into the tool's session tree. When the session ends, it unmounts.

## Prerequisites

- **WSL2** (Ubuntu 24.04) or native Linux
- **Python 3.11+**
- **Root privileges** (required for `mount`/`umount` syscalls)
- **curl** and **jq** (for running the test script)

## Quick Start

### 1. Install dependencies

```bash
pip install -r requirements.txt
```

### 2. Start the daemon

```bash
sudo python3 daemon.py
```

The server listens on `http://127.0.0.1:5000`.

### 3. Create a bind mount

```bash
curl -X POST http://127.0.0.1:5000/mount \
  -H "Content-Type: application/json" \
  -d '{"user": "testuser", "tool": "microscope1"}'
```

**Response** (HTTP 201):
```json
{
  "status": "ok",
  "message": "Bind mount created",
  "source": "/tmp/labdata/users/testuser",
  "target": "/tmp/labdata/sessions/microscope1/testuser"
}
```

### 4. Verify the mount

```bash
mount | grep labdata
```

### 5. Remove the bind mount

```bash
curl -X POST http://127.0.0.1:5000/unmount \
  -H "Content-Type: application/json" \
  -d '{"user": "testuser", "tool": "microscope1"}'
```

**Response** (HTTP 200):
```json
{
  "status": "ok",
  "message": "Bind mount removed",
  "target": "/tmp/labdata/sessions/microscope1/testuser"
}
```

### 6. Health check

```bash
curl http://127.0.0.1:5000/health
```

## API Reference

| Endpoint   | Method | Body                                      | Description               |
|------------|--------|-------------------------------------------|---------------------------|
| `/mount`   | POST   | `{"user": "<name>", "tool": "<name>"}`    | Create a bind mount       |
| `/unmount` | POST   | `{"user": "<name>", "tool": "<name>"}`    | Remove a bind mount       |
| `/health`  | GET    | —                                         | Liveness check            |

### Error Responses

| HTTP Code | Meaning                                    |
|-----------|--------------------------------------------|
| 400       | Missing/invalid fields, bad JSON, or path traversal attempt |
| 404       | Target path does not exist (unmount)       |
| 500       | Mount/unmount syscall failure              |

## Running Tests

With the daemon running in one terminal:

```bash
# Terminal 1
sudo python3 daemon.py

# Terminal 2
sudo bash test_daemon.sh
```

The test script covers:
1. Health check
2. Successful mount
3. Mount verification via `mount | grep`
4. Idempotent re-mount
5. Successful unmount
6. Unmount verification
7. Error handling (missing fields, invalid JSON, path traversal, unmount of nonexistent path)

## Design Decisions

- **No `subprocess` / `shell=True`**: Mount operations use the `mount(2)` and `umount(2)` syscalls directly via `ctypes`, eliminating shell injection risks.
- **Path traversal protection**: User and tool names are validated to reject `/`, `\`, `.`, and `..`.
- **Idempotent mounts**: Re-mounting an already-mounted path returns HTTP 200 instead of failing.
- **Directory auto-creation**: Both source and target directories are created with `mkdir -p` semantics.

## License

Internal — NEMO-CE Lab
