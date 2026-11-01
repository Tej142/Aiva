# 🚀 AIVA Oracle Cloud (Always Free) Deployment Guide

This guide will walk you through setting up a **high-performance, dedicated 24/7 cloud server for AIVA** with **4 Dedicated CPU Cores and 24 GB RAM for 100% free forever**.

---

## 📋 Overview of Specifications (Free Tier)
- **Compute**: 4 OCPUs (Ampere ARM A1 @ 3.0 GHz)
- **Memory**: 24 GB High-Speed RAM
- **Storage**: 200 GB NVMe SSD
- **Network**: Static Public IPv4 + 10 TB free monthly egress
- **Uptime**: 24/7/365 (Zero sleep / 0ms cold starts)

---

## 🛠️ Step 1: Create Your Free Oracle Cloud Account

1. Go to **[https://www.oracle.com/cloud/free/](https://www.oracle.com/cloud/free/)** and click **Start for free**.
2. **Account Details**:
   - Enter your Country (e.g. **India**).
   - Enter your Name and Email.
3. **Home Region Selection (CRITICAL)**:
   - Choose the region physically closest to you for the lowest ping latency:
     - **India South (Hyderabad)** or **India West (Mumbai)**.
   - *Note: Your Always Free 4 Core / 24GB allocation is locked to your selected Home Region.*
4. **Identity Verification**:
   - Complete mobile OTP verification.
   - Add a Debit/Credit card (Visa/Mastercard with international transactions enabled). Oracle performs a temporary test authorization (~₹80–100 / $1) which is **immediately refunded**.
5. Once your account is active, log in to the **Oracle Cloud Console**.

---

## 🖥️ Step 2: Create Your 4-Core, 24GB RAM Virtual Machine

1. In the Oracle Console search bar, type **Instances** and click **Instances (Compute)**.
2. Click **Create Instance**.
3. **Configure the Instance**:
   - **Name**: `aiva-cloud-server`
   - **Placement**: Keep default Availability Domain.
   - **Image and Shape**:
     - Click **Change Image** → Select **Ubuntu 24.04 LTS** (or Ubuntu 22.04 LTS) → Click **Select Image**.
     - Click **Change Shape**:
       - Select **Ampere (ARM)**.
       - Choose **VM.Standard.A1.Flex** (marked **Always Free Eligible**).
       - Drag the sliders to:
         - **OCPUs**: `4`
         - **Memory**: `24 GB`
       - Click **Select Shape**.
4. **Networking**:
   - Keep default VCN (Virtual Cloud Network) and Subnet.
   - Ensure **Assign a public IPv4 address** is checked (**Yes**).
5. **Add SSH Keys**:
   - Select **Generate a key pair for me**.
   - Click **Save Private Key** (download the `.key` file to your PC, e.g. in `C:\Users\cool\.ssh\oracle_aiva.key`).
   - Click **Save Public Key**.
6. **Boot Volume**:
   - Keep default (50 GB to 200 GB SSD).
7. Click **Create** at the bottom.
8. Wait ~1-2 minutes until the status turns **RUNNING** (Green icon).
9. Copy your **Public IP Address** (e.g., `129.154.XX.XX`).

---

## 🔓 Step 3: Open Network Ports in Oracle Cloud Firewall

By default, Oracle blocks incoming web traffic. We must open ports **80** (HTTP), **443** (HTTPS), and **5000** (AIVA Flask Server):

1. On your Instance Details page, under **Primary VNIC**, click your **Subnet** link (e.g., `subnet-...`).
2. Click **Default Security List for...**.
3. Click **Add Ingress Rules**:
   - **Source CIDR**: `0.0.0.0/0`
   - **IP Protocol**: `TCP`
   - **Destination Port Range**: `80,443,5000`
   - **Description**: `AIVA Web and Alexa Webhooks`
4. Click **Add Ingress Rules**.

---

## 💻 Step 4: Connect to Your Cloud Server via SSH

Open **PowerShell** or **Terminal** on your PC and run:

```bash
# Set secure permissions on your private key (Windows PowerShell)
icacls "C:\Users\cool\.ssh\oracle_aiva.key" /inheritance:r
icacls "C:\Users\cool\.ssh\oracle_aiva.key" /grant:r "$($env:USERNAME):(R)"

# Connect to your Ubuntu server (replace with your Oracle Public IP)
ssh -i "C:\Users\cool\.ssh\oracle_aiva.key" ubuntu@YOUR_ORACLE_PUBLIC_IP
```

---

## ⚡ Step 5: Install Dependencies & Setup AIVA on the Server

Once connected via SSH into your Ubuntu cloud server, run these commands:

```bash
# 1. Update system packages
sudo apt update && sudo apt upgrade -y

# 2. Open firewall ports on Ubuntu
sudo iptables -I INPUT 6 -m state --state NEW -p tcp --dport 80 -j ACCEPT
sudo iptables -I INPUT 6 -m state --state NEW -p tcp --dport 443 -j ACCEPT
sudo iptables -I INPUT 6 -m state --state NEW -p tcp --dport 5000 -j ACCEPT
sudo netfilter-persistent save

# 3. Install Python 3.11/3.12, Git, and Pip
sudo apt install -y python3 python3-pip python3-venv git curl

# 4. Clone AIVA (or upload your repository)
git clone https://github.com/YOUR_GITHUB_USERNAME/aiva.git /home/ubuntu/aiva
cd /home/ubuntu/aiva

# 5. Create Python virtual environment & install requirements
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
pip install gunicorn flask flask-cors mistralai google-genai groq tavily-python
```

---

## 🔄 Step 6: Configure AIVA as a 24/7 Background Service (`systemd`)

This ensures AIVA **always runs in the background**, restarts automatically if it crashes, and boots up automatically if the server restarts.

1. Create a systemd service file:
```bash
sudo nano /etc/systemd/system/aiva.service
```

2. Paste the following configuration:
```ini
[Unit]
Description=AIVA Alexa and Webhook Master Server
After=network.target

[Service]
User=ubuntu
WorkingDirectory=/home/ubuntu/aiva
ExecStart=/home/ubuntu/aiva/venv/bin/python aiva_server.py
Restart=always
RestartSec=3
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=multi-user.target
```

3. Enable and start the service:
```bash
sudo systemctl daemon-reload
sudo systemctl enable aiva
sudo systemctl start aiva

# Check that it's running:
sudo systemctl status aiva
```

---

## 🌐 Step 7: Free Permanent HTTPS Domain (For Alexa Skills)

Amazon Alexa requires a secure `https://` endpoint. You can get a free permanent HTTPS domain in 2 minutes using **Cloudflare** or **DuckDNS**:

### Method A: Free Cloudflare Tunnel (Zero Port-Forwarding Needed)
1. In your Oracle server terminal:
```bash
# Install Cloudflare tunnel daemon
curl -L --output cloudflared.deb https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-arm64.deb
sudo dpkg -i cloudflared.deb

# Run quick free tunnel to port 5000
cloudflared tunnel --url http://localhost:5000
```
2. Cloudflare will print a free permanent HTTPS URL:
   `https://random-subdomain.trycloudflare.com`

3. In **Amazon Developer Console → Alexa Skill → Endpoints**:
   - Select **HTTPS**.
   - Enter: `https://your-tunnel-url.trycloudflare.com/alexa`
   - Certificate type: **My development endpoint is a sub-domain of a domain that has a wildcard certificate from a certificate authority**.
   - Click **Save Endpoints**.

---

## 🎯 Finished!
Your AIVA Brain is now running **24/7 on a 4-Core, 24GB RAM dedicated cloud engine** for free with instant response times and zero maintenance!
