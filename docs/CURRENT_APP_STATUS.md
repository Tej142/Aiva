# AIVA System & Application Status Report

**Report Date:** November 1, 2026  
**Environment:** Production / Cloud + Local Companion  
**Cloud Instance:** Oracle Cloud VM (`140.245.254.83:5000`)  
**Repository Branch:** `origin/main`  
**Latest Commit:** `465da4c` (Overhaul: UI/UX, per-user data isolation, unified design system)

---

## 1. Executive Summary

AIVA is fully transitioned into an enterprise-grade, multi-tenant personal AI ecosystem. It features:
1. **Cloud Backend**: Deployed and active on Oracle Cloud Infrastructure under systemd supervisor (`aiva.service`), handling authenticated REST APIs, multi-tenant SQLite persistence, Brevo email OTP, and an outbound WebSocket command channel.
2. **Per-User Data Isolation**: Multi-tenant architecture with strict token-derived `user_id` scoping across all memory engines, conversation histories, learned task patterns, and settings. Existing user data was migrated to `charanpc1420091@gmail.com`; new users receive a clean-slate onboarding.
3. **Unified Design System**: Zero-dependency `tokens.css` with a refined dark-first aesthetic (`#0B0D12`), layered elevations, calm violet accent (`#6366F1`), light theme support, and locally bundled Inter `.woff2` typography (zero CDN dependencies).
4. **Windows Companion App ("AIVA Device Agent")**: Frameless Electron 33 application with custom draggable titlebar, 6-box auto-advancing OTP, hardware permission controls, pause tray toggle, local action allowlist, and packaged NSIS installer (82.2 MB).
5. **Quality & Verification**: 15/15 automated unit and isolation tests passing (`Ran 15 tests in 6.2s: OK`), 18 high-resolution UI screenshots captured across dark/light themes.

---

## 2. Cloud Server Infrastructure (Oracle Cloud VM)

| Metric / Parameter | Value / Status | Verification Method |
|---|---|---|
| **Public Host IP** | `140.245.254.83` | Direct SSH & HTTP probing |
| **Server Port** | `5000` (HTTP & WSS) | Live service listener |
| **Operating System** | Oracle Linux Server 9.4 (UEK Kernel 5.15.0) | `uname -r` |
| **Process Supervisor** | Systemd (`aiva.service`) | `systemctl is-active aiva` |
| **Service Status** | **Active (Running)** (PID 56865) | `systemctl status aiva` |
| **Process Memory** | ~43.0 MB (resident) | `systemctl status aiva` |
| **Installer Hosting** | `/download/windows-agent` (82.2 MB) | HTTP 200 attachment download |
| **Firewall (UFW/Firewalld)** | Port 5000 TCP `ACCEPT` enabled | `iptables` / Oracle Cloud Security List |

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
  - Max 5 failed OTP attempts before invalidation.
  - Constant-time secret comparison (`hmac.compare_digest`).
  - Passwords avoided in favor of passwordless email OTP; HMAC-SHA256 JWT access tokens (15m expiry) and cryptographically random refresh tokens (30d expiry).
  - Registration stores user full name and tracks `onboarded` state.

### B. Multi-Tenant Data Isolation (`services/database_service.py`, `kernel.py`)
- **Status:** **Operational & 100% Isolated**
- **Database Schema:** SQLite at `storage/aiva.db` with WAL mode and `busy_timeout = 5000`.
- **User Scoping:**
  - `user_memories (id, user_id, fact_key, fact_value, category, updated_at)`
  - `conversation_history (id, user_id, role, content, created_at)`
  - `user_task_patterns (id, user_id, trigger_pattern, goal_template, ...)`
  - `user_settings (user_id, setting_key, setting_value, updated_at)`
- **Security Guarantee:**
  - All endpoints derive `user_id` strictly from verified JWT tokens via `@require_auth`.
  - Client-supplied `user_id` parameters are ignored.
  - Kernel sets active user context (`kernel.set_active_user(user_id)`) per request.
  - Automatic migration script preserved all pre-existing data under Charan's account (`charanpc1420091@gmail.com`).
  - Verified by dedicated multi-user isolation test suite (`tests/test_data_isolation.py`).

### C. Device Registry & Real-Time Command Channel (`services/device_channel.py`)
- **Status:** **Operational**
- **Protocol:** Outbound WebSocket (`ws://` / `wss://` at `/ws/device`), device sends authenticated handshake with `device_token`.
- **Heartbeat:** Ping/pong every 30s with 10s timeout.
- **Routing:** AIVA actions route to target device by name or default to the most recently active online device. Clear, human-friendly error messages if no device is online.

### D. Windows Companion App ("AIVA Device Agent")
- **Status:** **Packaged & Verified**
- **Technology:** Electron 33, Node.js 20, native Windows DPAPI credential encryption.
- **UI Architecture:**
  - Frameless custom window with draggable header, user initials avatar, device indicator, and native window controls.
  - 6-box OTP input with auto-advance, clipboard paste, error shake animation, and 60s cooldown timer.
  - Slim navigation sidebar: Assistant, Devices, Activity, Settings.
  - System tray icon with "Pause control" toggle, show/hide, and quit handlers.
  - Packaged with NSIS installer (`windows-agent/dist/AIVA Device Agent Setup 1.0.0.exe`).

### E. Unified Design System (`tokens.css`)
- **Status:** **Deployed on Web HUD & Desktop App**
- **Theme:** Dark `#0B0D12` base with elevation layers, violet `#6366F1` primary accent, and light mode support (`[data-theme="light"]`).
- **Typography:** Bundled Inter woff2 (Regular, Medium, SemiBold, Bold) in `ui/fonts/` and `windows-agent/src/renderer/fonts/` (zero external CDN dependency).
- **Accessibility:** High-contrast focus rings, keyboard navigation, smooth motion with `prefers-reduced-motion` fallbacks.

---

## 4. Verification Suite & UI Screenshots

### Automated Test Suite (`python -m unittest discover tests`)
- `tests/test_data_isolation.py` (5 tests): Multi-user data isolation across memory, history, devices, and settings.
- `tests/test_device_e2e.py` (2 tests): End-to-end device registration, heartbeat, and tool dispatch.
- `tests/test_device_registry.py` (3 tests): Device creation, token generation, revocation, and rename.
- `tests/test_auth_service.py` (5 tests): OTP generation, validation, rate limiting, and token refresh.
- **Result:** **15 / 15 Tests Passed (100% pass rate).**

### Screenshot Catalog (`docs/screenshots/`)
| View | Dark Theme | Light Theme | Description |
|---|---|---|---|
| **Login** | `login_dark.png` | `login_light.png` | Email input with floating labels and branding panel |
| **Register** | `register_dark.png` | `register_light.png` | Name, email, and terms & conditions checkbox |
| **OTP Verification** | `otp_dark.png` | `otp_light.png` | 6-box digit input, masked email, and countdown |
| **Loading State** | `loading_dark.png` | `loading_light.png` | Branded spinner and personalized greeting |
| **Error Banner** | `error_dark.png` | `error_light.png` | Friendly error banner with input shake animation |
| **Assistant Home** | `home_dark.png` | `home_light.png` | Integrated Web HUD canvas with status pills |
| **Devices View** | `devices_dark.png` | `devices_light.png` | Active device cards, status badges, rename/revoke |
| **Activity Log** | `activity_dark.png` | `activity_light.png` | Execution audit log with status badges and latencies |
| **Settings & Permissions** | `settings_dark.png` | `settings_light.png` | Profile info, server URL, and capability toggles |

---

## 5. Deployment & Release Information

- **Web HUD URL:** `http://140.245.254.83:5000/`
- **Windows App Download:** `http://140.245.254.83:5000/download/windows-agent`
- **GitHub Repository:** `https://github.com/Tej142/Aiva.git`
