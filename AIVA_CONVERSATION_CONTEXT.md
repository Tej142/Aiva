# 🧠 AIVA System State & Conversation Continuity Guide

This document preserves the complete technical state, architectural decisions, completed milestones, and active roadmap of the **AIVA (Artificial Intelligence Virtual Assistant)** project. Any AI assistant or developer picking up this codebase can resume work seamlessly without context loss.

---

## 📌 1. Project Overview & Architecture
AIVA is an autonomous, multi-agent AI assistant designed for desktop automation, continuous voice conversation (via Amazon Echo Dot 3rd Gen), and sub-millisecond task execution.

### Core Architecture Components:
- **Runtime Kernel** ([core/runtime/kernel.py](file:///d:/aiva/core/runtime/kernel.py)): Manages the event bus, capability registry, agent manager, task queue, and scheduler.
- **Workflow & Planner Agent** ([core/agents/planner_agent.py](file:///d:/aiva/core/agents/planner_agent.py), [services/mistral_service.py](file:///d:/aiva/services/mistral_service.py)): Generates directed acyclic graph (DAG) workflows using Mistral, with instant Gemini and Groq fallbacks.
- **Task Pattern Learning Engine** ([services/task_pattern_service.py](file:///d:/aiva/services/task_pattern_service.py), [core/observation/workflow_observer.py](file:///d:/aiva/core/observation/workflow_observer.py)): Automatically observes successful workflows, generalizes variable slots (`{contact}`, `{message}`, `{query}`), and stores patterns in [memory/task_patterns.json](file:///d:/aiva/memory/task_patterns.json) for **`< 1ms` instant execution without LLM roundtrips**.
- **Universal UI Controller** ([tools/ui_controller.py](file:///d:/aiva/tools/ui_controller.py)): Generalized automation primitives (`focus_window`, `type_text`, `press_key`, `hotkey`, `async_wait`).
- **Server & Alexa Gateway** ([aiva_server.py](file:///d:/aiva/aiva_server.py)): Flask HTTP/REST + Alexa Skills Kit (ASK) webhook handling continuous conversation mode.

---

## ✅ 2. Completed Milestones & Solved Problems

### A. Amazon Echo Dot (3rd Gen) Continuous Conversation
- Implemented ASK request handling in `aiva_server.py` (`LaunchRequest`, `IntentRequest`, `AivaPromptIntent`).
- Configured continuous multi-turn session mode (`shouldEndSession: false`).
- Utterances map `"aiva"` phonetically (*"Eye-Vah"*).
- Setup guide documented in [HOW_TO_CONNECT_ALEXA.md](file:///d:/aiva/HOW_TO_CONNECT_ALEXA.md).

### B. Chrome Stealth & Anti-Bot Detection
- **Problem**: Opening Chrome via Selenium caused "Chrome is being controlled by automated test software" banners and Google reCAPTCHA bot blocks.
- **Solution**:
  - Configured native Windows OS browser dispatching (`os.startfile(url)`) in [services/browser_service.py](file:///d:/aiva/services/browser_service.py) and [tools/browser_tool.py](file:///d:/aiva/tools/browser_tool.py).
  - Added anti-automation flags (`--disable-blink-features=AutomationControlled`, `excludeSwitches: ["enable-automation"]`, and CDP `navigator.webdriver` removal).
  - Replaced isolated debug directory with the user's primary Chrome profile.

### C. Smart Browser Tab Deduplication
- **Problem**: Issuing repeat commands for WhatsApp/YouTube created dozens of duplicate tabs.
- **Solution**: `open_url` now checks if an existing window/tab (e.g. `WhatsApp`, `YouTube`, `Gmail`) is already open. If open, it activates the existing tab; if not, it opens a single new tab.

### D. Universal UI & Keyboard Automation
- **Problem**: Hardcoding custom tools for every specific app/website is unmaintainable.
- **Solution**: Built 5 generalized primitives in [tools/ui_controller.py](file:///d:/aiva/tools/ui_controller.py):
  1. `focus_window(title)`: Restores & activates the target app window via Win32 API.
  2. `type_text(text)`: Instant (<5ms), multilingual, emoji-safe clipboard typing (`Ctrl+V`).
  3. `press_key(key)`: Dispatches keypresses (`enter`, `esc`, `down`, `tab`, etc.).
  4. `hotkey(keys)`: Dispatches held-modifier keyboard shortcuts (`ctrl+alt+/`, `alt+tab`).
  5. `async_wait(seconds)`: Non-blocking asynchronous delay for page/app loading.

### E. WhatsApp & Chat Messaging Automation
- **Fixed In-Chat Search Bug**: Added `Escape` (count: 2) to exit any active in-chat search and reset to global search.
- **Fixed Contact Selection**: Added `Down` arrow key to highlight the top matched contact/group before pressing `Enter`.
- **Calibrated Timings**: 7.0s initial WhatsApp Web connection wait, 2.0s search filter wait, 2.0s chat composer focus wait.

---

## 📂 3. Key Files & Reference Paths

| Component | Path | Description |
| :--- | :--- | :--- |
| **Server** | [aiva_server.py](file:///d:/aiva/aiva_server.py) | Alexa + REST API endpoint |
| **CLI Runtime** | [main.py](file:///d:/aiva/main.py) | Interactive desktop terminal runtime |
| **Kernel** | [core/runtime/kernel.py](file:///d:/aiva/core/runtime/kernel.py) | Central capability & event registry |
| **Planner Prompt** | [services/mistral_service.py](file:///d:/aiva/services/mistral_service.py) | LLM Planner grammar & examples |
| **UI Controller** | [tools/ui_controller.py](file:///d:/aiva/tools/ui_controller.py) | Keyboard, hotkey, and window focus engine |
| **Task Patterns** | [memory/task_patterns.json](file:///d:/aiva/memory/task_patterns.json) | Persistent learned workflow memory |
| **App Config** | [config/windows_apps.json](file:///d:/aiva/config/windows_apps.json) | Windows application path mappings |
| **Secrets** | `config/secrets.json` | API keys for Mistral, Gemini, Groq, Tavily |
| **Alexa Model** | [alexa_skill_model.json](file:///d:/aiva/alexa_skill_model.json) | Amazon ASK Interaction Model schema |
| **Oracle Setup** | [ORACLE_CLOUD_SETUP.md](file:///d:/aiva/ORACLE_CLOUD_SETUP.md) | Dedicated Always Free VM setup guide |

---

## 🚀 4. Current State & Next Steps for Future Convos

### 1. Cloud Deployment Options:
- **Oracle Cloud (Always Free, 4 Cores / 24 GB RAM)**: Setup guide in [ORACLE_CLOUD_SETUP.md](file:///d:/aiva/ORACLE_CLOUD_SETUP.md). Requires enabling international e-commerce in banking app if debit card was declined.
- **Card-Free Cloud (Render / Cloudflare Tunnel)**: Can be deployed on Render.com or via a free Cloudflare Tunnel directly on the PC without entering any bank card details.

### 2. Running AIVA:
- **Interactive Terminal**: `py main.py`
- **Alexa & REST Background Server**: `python aiva_server.py`

---

## 💡 Prompt for Future AI Conversations:
When starting a new conversation, the user or assistant can refer to this file:
> *"Please read [AIVA_CONVERSATION_CONTEXT.md](file:///d:/aiva/AIVA_CONVERSATION_CONTEXT.md) to understand the full system state, recent bug fixes, and active roadmap, and continue assisting from where we left off."*
