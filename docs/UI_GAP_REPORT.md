# AIVA UI/UX & Data Isolation Gap Report

**Audit Date:** November 1, 2026  
**Auditor:** Senior Product Designer & Full-Stack Systems Engineer  
**Scope:** `windows-agent/src/renderer/*`, `ui/*`, `aiva_server.py`, and all persistent memory/storage layers.

---

## 1. Screen-by-Screen UI/UX & Design Audit

### A. Windows Companion App (`windows-agent/src/renderer/`)

#### 1. Authentication & Device Pairing (`login.html`)
- **Visual & Layout Deficiencies:**
  - Standard framed window with default Windows OS title bar instead of a frameless, native desktop experience (e.g., Raycast / Linear).
  - Monolithic single-column card centered on flat `#0a0e17`. Lacks a split-screen brand panel with product narrative, logo animation, and mesh gradient backdrop.
  - Raw HTML input boxes with basic borders; lacks floating label animations, focus transitions, and inline validation icons.
  - Exposes an editable "Cloud Server URL" field (`http://localhost:5000`) directly on the primary login card, looking like an unpolished developer utility rather than a consumer/prosumer app.
- **Workflow & Functional Gaps:**
  - **No Registration View:** There is only a login flow ("Send Verification Code"). No screen exists to collect the user's Full Name or present a mandatory "I agree to Terms & Privacy Policy" checkbox.
  - **Crude OTP Input:** 6 separate text boxes exist, but they lack error shake animations on invalid submission, do not automatically trigger submission when the 6th digit is typed, have no 60-second cooldown timer on "Resend Code" (allowing request spam), and do not show a masked email hint (e.g. `c***@gmail.com`).
  - **Missing States:** No skeleton loader or branded progress screen during transitions ("Loading your AIVA...").
  - **Typography & Icons:** Uses browser-fallback system fonts and unicode emojis (`🔐`, `←`, `🔄`) instead of bundled modern typography (`Inter` / `Geist`) and Lucide vector iconography.

#### 2. Main Assistant View (`main.html`)
- **Visual & Layout Deficiencies:**
  - Top bar is inconsistent with the embedded web HUD inside the iframe. Both have independent top bars, resulting in stacked headers, duplicate status badges, and conflicting aesthetics.
  - Lacks a native application sidebar (Navigation: Assistant, Devices, Activity, Settings).
  - Navigation between views is handled by navigating the entire Electron window to separate HTML pages (`window.loadFile('settings.html')`), destroying iframe DOM state, resetting chat session state, and causing a jarring visual flash.
- **Missing States:**
  - Shows a blank white or empty frame while the cloud server is loading or if the network is sluggish.
  - No native offline recovery screen or retry mechanism when the cloud endpoint is unreachable.
  - Top bar lacks user avatar (initials badge), active device name pill, and unified connection state indicator.

#### 3. Settings & Security View (`settings.html`)
- **Visual & Layout Deficiencies:**
  - Unstructured vertical list with basic styling.
  - Lacks tabbed organization: Account, Appearance, Startup, Permissions, and System About.
  - Permissions are presented with basic switches; dangerous capabilities have a simple red text tag (`Elevated`) but lack warning modals or interactive risk confirmations when toggled ON.
  - **Crude Activity Log:** Implemented as a plain HTML table. Lacks filter chips (All, Success, Failed, Apps, Media), timestamp formatting, latency indicators, and payload inspector drawers.

---

### B. Cloud Web Interface HUD (`ui/index.html`, `ui/style.css`, `ui/app.js`)

#### 1. Visual Polish & Consistency
- **Hardcoded User Data in Template:**
  - Line 90 of `ui/index.html` hardcodes `<strong id="user-display-name">Charan</strong>`. Any guest or second user opening the web interface sees Charan's name.
- **Typography & CDN Violations:**
  - Loads Google Fonts (`Inter` and `JetBrains Mono`) from `fonts.googleapis.com` via CDN. Fails offline requirements and causes layout shift (FOIT/FOUT).
- **Styling Architecture:**
  - Heavy neon glows, low-contrast text elements, and rigid dark theme values without CSS custom properties for a light theme.
  - Missing design system tokens (spacing, typography scale, elevation, border radii).
- **Authentication Wall Missing:**
  - The web HUD completely lacks an authentication view or modal. Anyone navigating to `http://140.245.254.83:5000/` immediately gains access to the live assistant, chat history, and memory inspection.

---

## 2. Data Isolation Audit (Critical Security & Multi-Tenancy Review)

| Data Store / File | Current Scope | Leakage / Risk | Multi-Tenancy Strategy |
|---|---|---|---|
| **`storage/aiva.db` (`users` table)** | Scoped | Missing `name` column. | Add `name` column to `users`. Store user's full name upon registration. |
| **`storage/aiva.db` (`devices` table)** | Scoped (`user_id`) | Low risk. Queries filter by `user_id`, but endpoints must strictly extract `user_id` from verified JWT. | Enforce `@require_auth` on all device CRUD APIs. |
| **`storage/aiva.db` (`command_audit_logs`)** | Scoped (`user_id`) | Medium. Currently lacks pagination and user-isolated API endpoint. | Expose `/api/audit-logs` strictly scoped to verified caller `user_id`. |
| **`memory/user_memory.json`** | **GLOBAL (Single Tenant)** | **CRITICAL LEAK:** Stores `"name": "charan"`, `"important_fact": "my name"`. Returned unconditionally by `/api/memory` to unauthenticated callers. | Move into SQLite table `user_memories (user_id, key, value_json, updated_at)` or per-user scoped directory `memory/users/<user_id>/`. |
| **`memory/conversation_memory.json`** | **GLOBAL (Single Tenant)** | **CRITICAL LEAK:** Stores global chat turns, topic entities, and historical dialogue. Shared across all visitors. | Scope conversation memory to `user_id`. Add `conversation_history` table in SQLite or user-scoped memory store. |
| **`memory/runtime_memory.json`** | **GLOBAL (Single Tenant)** | **HIGH LEAK:** Stores recent search queries, tasks, and goal actions globally. | Scope runtime state and research outcomes per `user_id`. |
| **`memory/task_patterns.json`** | **GLOBAL (Single Tenant)** | **MEDIUM LEAK:** Workflows learned by one user are shared system-wide. | Scope task recipes by `user_id` with optional system defaults. |
| **`memory/knowledge_memory.json`** | **GLOBAL (Single Tenant)** | Research summaries cached globally. | Scope user notes and queries by `user_id`. |
| **`aiva_server.py:api_chat()`** | **FALLBACK LEAK** | If no JWT is sent, falls back to `db.get_primary_user()` (Charan), executing commands under Charan's account! | Require authentication for all private operations; never silently impersonate the primary user for unauthenticated requests. |
| **In-Memory Cache & Session** | **LEAK ON LOGOUT** | Neither the web HUD nor the Electron app purges cached conversation turns or memory upon logout. | Implement explicit cache purging (`localStorage.clear()`, session reset, in-memory state wipe) on logout. |

---

## 3. HTTPS / WSS Infrastructure Analysis

### Current State:
- Server listens on plain HTTP/WS at port `5000` (`http://140.245.254.83:5000`).
- Tokens (`aiva_access_token`) and Brevo verification OTPs are transmitted over plaintext HTTP across the public internet.

### Requirements for Production HTTPS & WSS:
1. **SSL Termination via Reverse Proxy:**
   - Install **Caddy** (or Nginx) on the Oracle Cloud VM. Caddy provides zero-configuration automatic TLS certificates from Let's Encrypt / ZeroSSL and natively proxies HTTP/2, HTTPS, and WebSockets (`reverse_proxy localhost:5000`).
2. **Domain Configuration:**
   - Map a public DNS record (e.g. `aiva.<domain>.com` or a free DuckDNS domain such as `aiva-hub.duckdns.org`) to `140.245.254.83`.
3. **Firewall & Security Rules:**
   - Open Oracle Cloud Ingress Rules: TCP port `80` (HTTP challenge) and `443` (HTTPS/WSS).
   - Open VM firewalld: `sudo firewall-cmd --add-service=http --add-service=https --permanent && sudo firewall-cmd --reload`.
   - Restrict port `5000` to local loopback (`127.0.0.1:5000`) so external clients cannot bypass TLS.
4. **App Build-Time Security:**
   - Parameterize the companion app's default server URL via build configuration (`SERVER_URL=https://...`).
   - Disallow insecure `http://` / `ws://` schemes in production builds.

---

## 4. Phase-by-Phase Roadmap

1. **Phase 1: Per-User Data & Multi-Tenancy Backend**
   - SQLite migration: Add `name` to `users`. Create `user_memories` and `conversation_history` tables.
   - Backup `storage/aiva.db` to `storage/aiva.db.bak`.
   - Migrate existing data in `memory/user_memory.json` and `memory/conversation_memory.json` to Charan's user account (`charanpc1420091@gmail.com`).
   - Enforce verified `user_id` extracted from JWT in all memory, chat, device, and audit queries.
   - Write automated data isolation tests ensuring user A cannot access user B's records.

2. **Phase 2: Unified Design System (`tokens.css`)**
   - Build a shared, refined design system inspired by Linear/Raycast.
   - Dark theme `#0B0D12` + light theme + system preference toggle.
   - Bundle `Inter` locally (eliminate Google Fonts CDN).
   - Component library: 6-box OTP, floating label inputs, loading buttons, modals, toasts, risk badges, status pills, and Lucide icons.

3. **Phase 3: Auth Screens (Electron App)**
   - Frameless window with custom draggable title bar.
   - Split layout: Branded hero panel + clean form panel.
   - Register view (Full Name, Email, Terms checkbox) and Login view.
   - OTP view with auto-advance, paste-whole-code, 60s resend timer, error shake animation, and masked email.
   - Branded "Loading your AIVA..." skeleton transition.

4. **Phase 4: Main Application & Native Experience**
   - Multi-tab sidebar (Assistant HUD, Devices, Activity, Settings).
   - Frameless top bar with user initials avatar, device name, and connection pill.
   - Restyle embedded HUD to blend seamlessly into native frame.
   - Native device cards (rename, revoke, online indicator).
   - Activity log with filters and latency metrics.

5. **Phase 5: Web HUD Login & Parity**
   - Integrate authentication wall into `ui/index.html` with identical design tokens and flow.
   - Clean "Download Windows App" primary CTA.

6. **Phase 6: HTTPS & Reverse Proxy Setup**
   - Configure Caddy on Oracle VM for automatic HTTPS/WSS.
   - Update firewall rules and lock down port 5000 to localhost.
   - Enforce HTTPS in production Electron builds.

7. **Phase 7: Comprehensive Verification & Packaging**
   - Run automated isolation tests.
   - Document screenshots of all screens in dark and light modes.
   - Build updated installer without leaked secrets.
