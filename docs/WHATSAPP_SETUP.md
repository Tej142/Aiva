# AIVA WhatsApp Channel Setup & Operation Guide

This guide describes how to configure, pair, and run the cost-free WhatsApp channel for AIVA using Baileys (`@whiskeysockets/baileys`) as a linked device.

---

## 1. Overview & Architecture

- **Dedicated AIVA Account (`AIVA_WHATSAPP_NUMBER`)**: The Baileys bridge pairs exclusively with AIVA's dedicated WhatsApp number (`AIVA_WHATSAPP_NUMBER`) and refuses startup/relay if paired to any other account. Each user links their own personal phone number via the HUD or WhatsApp onboarding.
- **Admin Governance (`AIVA_ADMIN_EMAIL`)**: Only the account whose email matches `AIVA_ADMIN_EMAIL` holds admin privileges (bridge pairing/logout, user approve/deny/revoke, phone block/unblock, third-party `whatsapp_send` when restricted).
- **Node Bridge (`services/whatsapp-bridge/`)**: Runs locally on `127.0.0.1:3001` (`index.js`, `pairing.js`, `pair.js`) managing the Baileys WebSocket, media lazy-download (`/media/download`), and read receipts (`/read`).
- **Python Control Plane (`services/whatsapp_config.py`, `services/whatsapp_service.py`, `services/whatsapp_notifier.py`, `aiva_server.py`)**: Deterministic credential challenges, mtime-cached `.env` hot-reloading, server-side `gate_state` middleware + `/ws/device` enforcement, DB-backed sessions, and metadata-separated third-party inbox alerts.
- **Zero-PII Loopback Security**: All `/internal/*` bridge calls are restricted to `127.0.0.1` with constant-time `X-Bridge-Secret` validation. Challenges, credentials, message bodies, and full phone numbers never appear in logs.

---

## 2. Environment Configuration (`.env`)

Configure `.env` on the server (**never commit** `.env`; lock permissions with `chmod 600 .env`).  
Edits to `.env` take effect automatically via `mtime`-cached reloading without restarting AIVA. Quoted values support spaces:

```bash
WHATSAPP_ENABLED=true
AIVA_WHATSAPP_NUMBER=919876543210
AIVA_ADMIN_EMAIL=charanpc1420091@gmail.com
WHATSAPP_BRIDGE_PORT=3001
WHATSAPP_BRIDGE_SECRET=generate_a_random_64_character_hex_secret_here
WHATSAPP_BACKEND_URL=http://127.0.0.1:5000

# Deterministic shared credential challenge (optional AIVA_ACCESS_PASSWORD_HASH overrides password)
AIVA_ACCESS_USERNAME="team_user"
AIVA_ACCESS_PASSWORD="change_this_shared_password"
AIVA_ACCESS_PASSWORD_HASH=
AIVA_ACCESS_EPOCH=1
AIVA_ACCESS_MAX_ATTEMPTS=3
AIVA_ACCESS_LOCK_MINUTES=30
AIVA_ACCESS_CHALLENGE_TTL_MINUTES=5
AIVA_ACCESS_MAX_CHALLENGES_PER_HOUR=20
AIVA_REQUIRE_OWNER_APPROVAL=false

# Entry B & User Limits
WHATSAPP_VERIFY_MODE=inbound
WHATSAPP_MAX_VERIFY_SENDS_PER_DAY=3
AIVA_USER_DAILY_MESSAGE_CAP=100
AIVA_USER_CAN_SEND_WHATSAPP=false
WHATSAPP_ALLOW_POWER_CONTROL=false

# Sessions & Notifier
WHATSAPP_START_TRIGGER="aiva\\"
WHATSAPP_STOP_TRIGGER="stop\\"
WHATSAPP_SESSION_IDLE_MINUTES=60
WHATSAPP_REPLY_WINDOW_MINUTES=10
WHATSAPP_QUIET_HOURS_START=23:00
WHATSAPP_QUIET_HOURS_END=07:00
WHATSAPP_INBOX_RETENTION_DAYS=7
WHATSAPP_MARK_READ_ON_READ=true
```

---

## 3. Step-by-Step Installation on Oracle Cloud VM

Connect to your Oracle Cloud VM via SSH:
```bash
ssh -i /path/to/ssh-key.key opc@140.245.254.83
cd ~/aiva
```

### Step 3.1: Install Node.js Dependencies
```bash
cd services/whatsapp-bridge
npm install
```

### Step 3.2: Pair AIVA with Your Phone
Run the headless pairing utility:
```bash
npm run pair
```

- If `AIVA_WHATSAPP_NUMBER` is set in `.env`, the script will request an 8-digit pairing code from WhatsApp (e.g. `ABCD-1234`).
- On your phone:
  1. Open WhatsApp -> **Settings** -> **Linked Devices**.
  2. Tap **Link a Device**.
  3. Tap **Link with phone number instead**.
  4. Type the 8-character pairing code shown on your terminal.
- Once connected, the tool will save the credentials to `storage/whatsapp_auth/` and exit with success.

*(Note: If you run `npm run pair` without `AIVA_WHATSAPP_NUMBER`, a terminal QR code will be displayed for scanning instead).*

### Step 3.3: Deploy Systemd Service
To ensure the WhatsApp bridge stays connected indefinitely and auto-restarts on reboot:

```bash
sudo cp deploy/aiva-whatsapp.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable aiva-whatsapp
sudo systemctl start aiva-whatsapp
sudo systemctl status aiva-whatsapp
```

---

## 4. Onboarding, Verification Gate & Daily Commands

### Entry A — WhatsApp-First Onboarding
1. From an unlinked phone, message `aiva\` (or `aiva/`, case-insensitive) to AIVA's number.
2. Complete the deterministic credential challenge (`username`, then `password`; delete those chat messages afterward).
3. Enter your explicit display name (*"What should I call you?"*), stored directly in `users.name`.
4. Your number is linked and an `ACTIVE` chat session begins immediately (or enters `approval_pending` if `AIVA_REQUIRE_OWNER_APPROVAL=true`).

### Entry B — HUD Verification Gate
1. New email-OTP accounts start in `number_pending` (`403 verification_required` across `/api/*` and `/ws/device` until active).
2. In the HUD Verification screen, enter your personal E.164 phone number and click **Send on WhatsApp** to send the generated `AIVA-XXXXXX` code via the `wa.me` button (or enter a code sent via WhatsApp if `WHATSAPP_VERIFY_MODE=outbound`).
3. **Owner Exemption:** The `AIVA_ADMIN_EMAIL` account unlocks immediately upon matching code arrival; all other users complete the shared credential challenge to unlock the HUD live over `/ws/hud`.

### Sessions, Inbox & Commands
- **Session Lifecycle (`INACTIVE` / `ACTIVE`):** Verified users send `aiva\` (or `aiva\ <command>`) to start chatting, and `stop\` (or HUD **End Session**) to end. Idle sessions auto-close after `WHATSAPP_SESSION_IDLE_MINUTES` with a single notice.
- **Third-Party Messages & Voice Notes:** Messages from a user's saved contacts are stored in `whatsapp_inbox` with **metadata-only** debounced alerts (or quiet-hours morning digests unless the contact is marked **Priority**). Reading inbox items requires explicit user intent (`whatsapp_read_messages`), lazily transcribes voice notes while wiping temp files, posts `/read` receipts only post-read, and fences text in `<whatsapp_message untrusted="true">`.
- **Contact & Chat Commands:** `HELP`, `STATUS`, `save <number> as <name>`, `add alias <alias> for <name>`, `list contacts`, `mute/unmute <name>`, `priority/unpriority <name>`, `message <contact> <text>`.
- **Admin Governance & Epoch Rotation:** In WhatsApp or the HUD **Admin** tab: `list users`, `approve <user>`, `deny <user>`, `revoke <user>`, `block <phone>`, `unblock <phone>`. Revoking bumps `token_version` and drops HTTP/WebSocket sessions immediately. Increment `AIVA_ACCESS_EPOCH` in `.env` at any time to force all non-admin WhatsApp users to re-verify against updated credentials.

---

## 5. Maintenance & Troubleshooting

### View Bridge Logs
```bash
journalctl -u aiva-whatsapp -f
```

### Re-pairing After Logout (Error 401)
If WhatsApp invalidates your linked device session (e.g. logged out from phone):
1. Stop the bridge:
   ```bash
   sudo systemctl stop aiva-whatsapp
   ```
2. Clear old auth state:
   ```bash
   rm -rf storage/whatsapp_auth/*
   ```
3. Re-pair:
   ```bash
   cd services/whatsapp-bridge && npm run pair
   ```
4. Start the service again:
   ```bash
   sudo systemctl start aiva-whatsapp
   ```

### Check Bridge Health via Curl
```bash
curl -H "X-Bridge-Secret: YOUR_SECRET" http://127.0.0.1:3001/status
```
Expected output:
```json
{"connected":true,"status":"connected","phone":"919876543210","error":null}
```

