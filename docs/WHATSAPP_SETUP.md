# AIVA WhatsApp Channel Setup & Operation Guide

This guide describes how to configure, pair, and run the cost-free WhatsApp channel for AIVA using Baileys (`@whiskeysockets/baileys`) as a linked device.

---

## 1. Overview & Architecture

- **No Official Cloud API Cost**: Uses your existing phone number connected via Baileys Web multi-device protocol.
- **Node Bridge (`services/whatsapp-bridge/`)**: Runs locally on `127.0.0.1:3001`, managing the persistent Baileys WebSocket connection and media handling.
- **Python Backend (`services/whatsapp_service.py` & `aiva_server.py`)**: Handles linking, contact resolution, conversation routing, command execution, and rate limiting.
- **Loopback Security**: All internal communication is restricted to `127.0.0.1` and authenticated using constant-time `X-Bridge-Secret` token verification.
- **Per-User Isolation**: Phone links and contacts are strictly scoped to authenticated user IDs in SQLite (`aiva.db`). Unknown senders cannot trigger LLMs or device tools.

---

## 2. Environment Configuration (`.env`)

Add or update the following variables in your `.env` file on the server (do **never** commit this file):

```bash
# Enable WhatsApp subsystem
WHATSAPP_ENABLED=true

# The existing phone number AIVA runs on (digits only with country code, no +)
AIVA_WHATSAPP_NUMBER=919876543210

# The AIVA account email that self-messages ("Message yourself") map to
WHATSAPP_OWNER_EMAIL=charanpc1420091@gmail.com

# Node bridge port and shared internal secret
WHATSAPP_BRIDGE_PORT=3001
WHATSAPP_BRIDGE_SECRET=generate_a_random_64_character_hex_secret_here

# Backend loopback URL
WHATSAPP_BACKEND_URL=http://127.0.0.1:5000

# Human-like delay and rate limiting
WHATSAPP_REPLY_DELAY_MS=700
WHATSAPP_MAX_OUTBOUND_PER_HOUR=20
WHATSAPP_MAX_MESSAGE_CHARS=1500
WHATSAPP_ALLOW_POWER_CONTROL=false
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

## 4. User Linking & Daily Commands

### Linking a User Account
1. Open the Web HUD -> Navigate to the **WhatsApp** tab.
2. Enter your personal phone number (with country code, e.g. `919876543210`) and click **Generate Code**.
3. A 6-digit code (valid for 10 minutes) will be generated (e.g. `839201`).
4. From that phone, send `LINK 839201` to AIVA's WhatsApp number.
5. AIVA will verify the code and respond confirming that your account is linked.

### Chat & Voice Commands
- **Direct Messaging**:
  - *"message dad to come early this evening"*
  - *"tell mom I will be late for dinner"*
- **Research & Forward**:
  - *"search about Government Polytechnic, Tirupati and send the details to my second number"*
- **Contact Management in Chat**:
  - *"save 9876543210 as dad"*
  - *"add alias papa for dad"*
  - *"list my contacts"*
- **System Commands**:
  - `help` — Show available commands.
  - `status` — Check bridge connection and linked account status.
  - `unlink` or `stop` — Disconnect your phone from AIVA.

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

