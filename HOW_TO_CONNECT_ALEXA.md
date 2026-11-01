# 🎙️ Complete Step-by-Step Guide: Connect Echo Dot (3rd Gen) to AIVA

This guide walks you through connecting your **Amazon Echo Dot (3rd Gen)** to **AIVA** running on your PC. 

Once connected, saying **"Alexa, open AIVA"** turns your Echo Dot into an active, continuous AI terminal with hands-free conversation and full PC control!

---

## 📋 What You Need Before Starting
1. Your PC connected to the internet with AIVA codebase.
2. Your **Echo Dot (3rd Gen)** plugged in and connected to your home Wi-Fi.
3. Your Amazon account (the same one logged into your Alexa mobile app).
4. A free Amazon Developer account (takes 30 seconds to sign in).

---

## ⚡ Step 1: Start the AIVA Server on Your PC

1. Open your terminal (PowerShell / Command Prompt / Terminal in VS Code) in the `d:\aiva` folder.
2. Run this command:
   ```bash
   python aiva_server.py
   ```
3. You should see:
   ```text
   ==================================================
   🚀 AIVA ALEXA & WEBHOOK SERVER
   ==================================================
   [1/2] Initializing AIVA Runtime Kernel in background...
   [2/2] Starting Flask HTTP Server on port 5000...
   📌 Alexa Endpoint: http://localhost:5000/alexa
   ```
   > 💡 **Keep this terminal window open.**

---

## 🌐 Step 2: Create a Free HTTPS Tunnel (ngrok)

Amazon requires a secure HTTPS link to send requests to your local PC. We use `ngrok` (free).

1. Open a **new, second terminal** window.
2. Run:
   ```bash
   npx ngrok http 5000
   ```
   *(If you have ngrok installed directly, you can just run `ngrok http 5000`)*.
3. You will see an output like:
   ```text
   Forwarding   https://a1b2-c3d4-e5f6.ngrok-free.app -> http://localhost:5000
   ```
4. **Copy your `https://....ngrok-free.app` URL.** You will need it in Step 5.
   > 💡 **Keep this terminal window open while using Alexa.**

---

## 🛠️ Step 3: Create a Free Alexa Skill in Amazon Developer Console

1. Open your browser and go to:
   👉 **[https://developer.amazon.com/alexa/console/ask](https://developer.amazon.com/alexa/console/ask)**
2. Sign in with the **same Amazon account** that is logged into your Echo Dot / Alexa App.
3. Click the blue **Create Skill** button (top right).
4. Fill in these basic settings:
   * **Skill name**: `AIVA`
   * **Primary locale**: Select `English (US)` or `English (IN)` (match your Echo Dot's language).
   * **Experience**: Select **Other** $\rightarrow$ **Custom**.
   * **Hosting service**: Select **Provision your own** (do NOT choose Alexa-hosted).
5. Click **Next** (top right), then choose **Start from Scratch** template, and click **Create Skill**.

---

## 📄 Step 4: Import the AIVA Interaction Model (1-Click)

1. On the left sidebar of the Alexa Console, click **Interaction Model** $\rightarrow$ **JSON Editor**.
2. Open the file [`alexa_skill_model.json`](file:///d:/aiva/alexa_skill_model.json) in your project.
3. Copy **everything** inside `alexa_skill_model.json` and paste it into the JSON Editor in the browser (replacing whatever was there).
4. Click **Save Model** (top menu bar).
5. Click **Build Model** (top menu bar).
   *(Wait ~30–45 seconds until you see "Model built successfully").*

---

## 🔗 Step 5: Link Your HTTPS Endpoint

1. On the left sidebar, click **Endpoint**.
2. Select the **HTTPS** radio button.
3. Under **Default Region**, paste your ngrok URL from Step 2 followed by `/alexa`:
   ```text
   https://YOUR-SUBDOMAIN.ngrok-free.app/alexa
   ```
   *(Example: `https://a1b2-c3d4-e5f6.ngrok-free.app/alexa`)*
4. Under the SSL certificate dropdown, select:
   👉 **"My development endpoint is a sub-domain of a domain that has a wildcard certificate from a certificate authority"**
5. Click **Save Endpoints** (top menu bar).

---

## 🎙️ Step 6: Test on Your Echo Dot (3rd Gen)

### 1. Enable Development Mode:
1. In the top navigation bar of the Alexa Console, click on the **Test** tab.
2. Find the dropdown labeled **"Skill testing is enabled in:"** (it says "Off" by default).
3. Change it from **"Off"** to **"Development"**.

### 2. Talk to Your Physical Echo Dot:
Because the skill is enabled under your Amazon developer account, your **physical Echo Dot 3rd Gen** will automatically recognize the skill!

Walk up to your Echo Dot and say:
> 🗣️ **"Alexa, open AIVA"**

---

## 💬 Example Voice Conversations in AIVA Mode

Once you say *"Alexa, open AIVA"*, the blue ring stays active for continuous multi-turn dialogue:

| What You Say to Echo Dot | What AIVA Does |
| :--- | :--- |
| *"Open YouTube and search for Lo-Fi music"* | Chrome launches on your PC and opens the search results. |
| *"Open Notepad"* | Windows Notepad launches instantly on your PC. |
| *"Research Artificial Intelligence deeply"* | Performs multi-source web search via Tavily and speaks the summary back to you. |
| *"Remember that my favorite game is GTA"* | Saves your fact into `memory/user_memory.json`. |
| *"Tell me what is my name"* | Recalls your name from memory and speaks it. |
| *"Set volume to 50 percent"* | Adjusts your Windows PC master volume. |
| *"Stop"* / *"Exit"* / *"Goodbye"* | Closes the AIVA mode and turns off the blue ring. |

---

## 🔧 Helpful Tips & Troubleshooting

1. **If you restart ngrok:**
   * Free ngrok URLs change each time you start it. If you restart ngrok, just copy the new `https://....ngrok-free.app/alexa` link, paste it in the **Endpoint** tab, and click **Save Endpoints**.
2. **If Alexa says "There was a problem with the requested skill's response":**
   * Check your `aiva_server.py` terminal window to ensure the server is running.
   * Verify your ngrok URL has `/alexa` at the end.
3. **If your Echo Dot doesn't respond to "open AIVA":**
   * Double check that the Amazon account logged into [developer.amazon.com](https://developer.amazon.com) is the **exact same account** linked to your Alexa mobile app.
   * Ensure Test Mode is set to **"Development"** in the Alexa Console.
