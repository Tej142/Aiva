# AIVA System & Application Status Report

**Report Date:** November 1, 2026  
**Environment:** Production / Cloud + Local Companion  
**Cloud Instance:** Oracle Cloud VM (`140.245.254.83:5000`)  
**Repository Branch:** `origin/main` (commit `c3dfb7f`)  

---

## 1. Executive Summary

AIVA has been successfully transitioned from a standalone local desktop agent into a multi-device, cloud-hosted architecture. The cloud backend is fully deployed, active, and healthy on Oracle Cloud Infrastructure, featuring authenticated outbound WebSocket routing for hardware commands. The Windows Companion App ("AIVA Device Agent") is built with full security allowlists, Windows DPAPI credential encryption, and an installer ready for distribution.

---

## 2. Cloud Server Infrastructure (Oracle Cloud VM)

| Metric / Parameter | Value / Status | Verification Method |
|---|---|---|
| **Public Host IP** | `140.245.254.83` | Direct SSH & HTTP probing |
| **Server Port** | `5000` (HTTP & WSS) | Live service listener |
| **Operating System** | Oracle Linux Server 9.4 (UEK Kernel 5.15.0) | `uname -r` |
| **Process Supervisor** | Systemd (`aiva.service`) | `systemctl is-active aiva` |
| **Service Status** | **Active (Running)** (PID 55108) | `systemctl status aiva` |
| **Process Memory** | ~125.8 MB | Resident set size |
| **Available Disk** | 18 GB free on root filesystem | `df -h /` |
| **Firewall (UFW/Firewalld)** | Port 5000 TCP `ACCEPT` enabled | `firewall-cmd --list-ports` |

### Live Healthcheck Response
```json
GET http://140.245.254.83:5000/health
HTTP/1.1 200 OK
{
  "status": "healthy",
  "kernel_active": true,
  "devices_online": 0
}
```

---

## 3. Subsystem Breakdown & Status

### A. Authentication & Email OTP (`services/auth_service.py`)
- **Status:** **Operational & Verified Live**
- **Email Service Provider:** Brevo Transactional Email API (`POST https://api.brevo.com/v3/smtp/email`)
- **Sender Account:** `charanpc1420091@gmail.com`
- **Security:**
  - 6-digit cryptographically secure OTP with a 10-minute expiry window.
  - Rate limiting enforced (maximum 5 requests per 10-minute window per IP/email).
  - OTPs stored as SHA-256 hashes in SQLite; raw values never saved or returned in responses.
  - JWT Access Token (15-minute expiration) with rotating cryptographically random Refresh Tokens.
- **Verification:** Live end-to-end OTP dispatch confirmed successfully to user email on production host.

### B. SQLite Database (`services/database_service.py`)
- **Status:** **Operational**
- **Storage Path:** `storage/aiva.db` (Write-Ahead Logging / WAL enabled)
- **Active Tables:**
  - `users`: Core account identity and registration timestamps.
  - `otps`: Email verification codes with attempt tracking and expiry.
  - `refresh_tokens`: Revocable token rotation store.
  - `devices`: Multi-device pairing registry with SHA-256 token hashes and status tracking.
  - `command_audit_logs`: Audit trail of every dispatched command, target device, execution latency, and return status.

### C. Device Registry & WebSocket Channel (`services/device_manager.py` & `services/device_channel.py`)
- **Status:** **Operational**
- **Protocol:** Outbound WebSocket (`/ws/device`) with ping-pong heartbeats every 30s.
- **Routing Intelligence:**
  - Dynamic resolution targeting specific device names or defaulting to the user's most recently active online device.
  - Explicit diagnostic error feedback when no device is online (avoids silent failures in LLM tool execution).
- **Audit Logging:** Every command ID, payload, status code, and latency is recorded in `command_audit_logs`.

### D. Kernel Desktop Capability Dispatcher (`core/runtime/kernel.py`)
- **Status:** **Operational**
- **Capabilities Re-routed through Device Channel:**
  - `open_url` / YouTube / browser navigation
  - `launch_app` (system allowlist mapping)
  - `volume_control` (set percentage / mute)
  - `media_control` (play / pause / next / prev)
  - `take_screenshot` (base64 image payload transmission)
  - `type_text` (keystroke simulation)
  - `lock_screen` (workstation lock)
  - `list_running_apps` & `close_app`
  - `file_explorer` / `open_folder` (scoped paths)

### E. Web UI & Direct Binary Downloads (`ui/` & `aiva_server.py`)
- **Status:** **Operational**
- **Web Interface:** Accessible at `http://140.245.254.83:5000/`
- **Application Download Button:** Present on top-right navigation bar of the Web HUD.
- **Direct Installer Route:** `http://140.245.254.83:5000/download/windows-agent` serves the 81.7 MB executable directly with an automated fallback redirect to GitHub Releases.

---

## 4. Windows Companion App (`windows-agent/`)

| Module | Purpose | Status |
|---|---|---|
| **`src/main/index.js`** | Electron lifecycle, system tray integration, background minimize | Verified |
| **`src/main/secure-store.js`** | Token storage encrypted with Windows DPAPI via Electron `safeStorage` | Verified |
| **`src/main/permissions.js`** | Capability flags (Apps, Input, Media, Files, Screenshot, Power) | Verified |
| **`src/main/executor.js`** | Strict allowlist executor; native Windows confirmation modal for destructive tasks | Verified |
| **`src/main/ws-client.js`** | Outbound WebSocket connection with auto-reconnect backoff & acks | Verified |
| **`src/renderer/`** | Multi-screen UI: Login (Email + OTP), Embedded Web HUD, Settings & Live Log | Verified |
| **Installer (`dist/`)** | NSIS Setup Executable: `AIVA Device Agent Setup 1.0.0.exe` (81.7 MB) | Built & Deployed |

---

## 5. Verification & Test Matrix

| Test Suite / Check | Scope | Result |
|---|---|---|
| **`tests/test_auth_service.py`** | OTP generation, rate limiting, expiry, token rotation | **100% Passed (4/4)** |
| **`tests/test_device_registry.py`** | Device pairing, token generation, rename, revocation | **100% Passed (3/3)** |
| **`tests/test_device_channel.py`** | WebSocket routing, offline detection, cross-user isolation | **100% Passed (3/3)** |
| **Live Remote Endpoint Test** | `http://140.245.254.83:5000/health` | **200 OK** |
| **Live Brevo Email Test** | Production dispatch of OTP to `charanpc1420091@gmail.com` | **200 OK (Delivered)** |
| **Live Installer Download** | `http://140.245.254.83:5000/download/windows-agent` | **200 OK (81.7 MB)** |
| **Repository Secret Audit** | Scan `.env`, git history, and build artifacts for exposed secrets | **Clean (Zero Secrets Leaked)** |

---

## 6. How to Connect a New Windows PC

1. **Download the Agent:**
   - Click the **"Download Application"** button in the web HUD at `http://140.245.254.83:5000/` (or directly browse to `http://140.245.254.83:5000/download/windows-agent`).
2. **Install & Run:**
   - Run `AIVA-Device-Agent-Setup-1.0.0.exe`.
   - The app launches to the Login screen.
3. **Authenticate:**
   - Enter your email (`charanpc1420091@gmail.com`).
   - Check your inbox for the 6-digit OTP code sent via Brevo.
   - Enter the OTP to sign in.
4. **Operation:**
   - The embedded AIVA Web HUD appears automatically with your session active.
   - The app minimizes to the system tray on close and maintains its real-time command link.
   - Any voice or text command directed to AIVA (e.g., *"open YouTube"*, *"open Notepad"*, *"set volume to 40"*) will immediately execute on your local Windows PC.

