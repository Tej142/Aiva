# SYSTEM SPECIFICATION & ARCHITECTURE CONTEXT: AIVA
> **Living Document for LLM Prompt Generation & Context Ingestion**  
> **Last Updated:** 2026-11-11  
> **Version:** 2.2.0 (Jarvis-Grade Voice, Unified Memory Engine & Windows Companion Agent)

---

## 1. System Overview & Identity

**AIVA** (Artificial Intelligence Virtual Assistant) is a Jarvis-grade, event-driven personal AI operating system featuring multi-device hardware orchestration, autonomous multi-agent DAG planning, a low-latency conversational voice pipeline, and persistent multi-tiered memory.

### Hybrid Split Topology
- **Cloud Backend:** Headless Python micro-kernel running on an Oracle Cloud Infrastructure Linux VM (`opc@140.245.254.83`) managed via systemd (`aiva.service`). Exposed via HTTPS/WSS through reverse proxy at `https://aivaserve.duckdns.org` (and direct `http://140.245.254.83:5000`).
- **Web HUD:** Responsive glassmorphism web interface (vanilla JS, HTML5, CSS3 with `tokens.css` indigo-to-cyan theme). Can be run in any browser or embedded in client frames.
- **Windows Companion App ("AIVA Device Agent"):** Frameless Electron 33 desktop app running on Windows 10/11 on the user's PC (`CHARAN-LAP`). Embeds the Web HUD via iframe and executes local OS operations through a secure bidirectional WebSocket channel.

---

## 2. Architecture & Technology Stack

| Layer | Technologies & Frameworks | Key Responsibilities |
|---|---|---|
| **Cloud Backend** | Python 3.11, Flask, WebSockets (`websockets`, `gevent`/`threading`), SQLite | API endpoints, intent classification, multi-LLM failover, user isolation, auth, device coordination |
| **LLM Engine** | Multi-Provider pool: Groq (`openai/gpt-oss-20b`, `openai/gpt-oss-120b`), Gemini (`gemini-3-flash-preview`, `gemini-3.1-flash-lite`, `gemini-2.5-flash-lite`), Mistral | Conversation, compound intent parsing, native tool calling, DAG step planning, 429/404 model fallbacks |
| **STT & TTS** | Groq Whisper `whisper-large-v3`, Microsoft Edge Neural TTS (`edge-tts 7.3.1`) | High-speed cloud audio transcription, human-like voice synthesis (`en-IN-PrabhatNeural` / `en-US-AriaNeural`) |
| **Desktop Companion** | Electron 33, Node.js 20/24, Windows SAPI (`System.Speech`), Windows API, `ws` | Local execution, global shortcuts, screen capture, Start Menu app discovery, native offline wake word |
| **Voice Runtime** | Web Audio API, AnalyserNode, MediaRecorder, in-memory PowerShell bytecode | Client-side VAD, 60fps animated Neural Orb, zero-latency earcons, 6s follow-up window, barge-in |
| **Storage & Auth** | SQLite (`storage/aiva.db`), JWT Bearer tokens (15m access / 30d refresh), DPAPI SecureStore | 6-digit OTP email login, encrypted token persistence, per-user data isolation |

---

## 3. Core Subsystems & Pipelines

### A. Jarvis-Style Voice Pipeline
1. **Native Offline Wake Word ("Hey AIVA" / "Jarvis"):**
   - Implemented in `windows-agent/src/main/wake-word-engine.js`.
   - Executes an in-memory PowerShell script leveraging native .NET `System.Speech.Recognition` with zero disk file dependencies and zero cloud tokens.
   - Low CPU footprint (<0.5%) with audio input set to the default recording device.
   - Active keywords: `"Hey AIVA"`, `"AIVA"`, `"Hi AIVA"`, `"Ok AIVA"`, `"Hey Jarvis"`, `"Jarvis"`, `"Hey Ava"`, `"Ava"`, `"Hey Eva"`, `"Eva"`, `"Hey Iva"`, `"Iva"`, `"Computer"`.
   - Global Hotkey fallback: `Ctrl+Space` (Push-To-Talk) triggers voice even when minimized to the system tray.
2. **Unified State Machine (`VoiceStateMachine` in `ui/voice-manager.js`):**
   - Single source of truth: `IDLE` -> `LISTENING` -> `PROCESSING` -> `SPEAKING` -> `FOLLOW_UP` -> `IDLE` (plus `MUTED` and `ERROR`).
   - Auto-Recovery: Any error state automatically recovers to `IDLE` after 2.2 seconds to prevent locking the Central Orb in red.
3. **Dialogue Flow & Continuous Turn-Taking:**
   - **Real-Time VAD:** Measures RMS energy at 60fps via `AnalyserNode`. Configured with `rmsThreshold: 0.022`, 800ms natural pause tolerance, and 350ms minimum speech filter.
   - **Continuous Follow-Up Window:** After TTS audio completes, the mic automatically remains live for **6 seconds** (`FOLLOW_UP` state). If the user speaks again, VAD switches to `LISTENING` without needing the wake word. Silence times out with a gentle sleep chime.
   - **Barge-In:** If the user speaks while AIVA is outputting audio, playback halts in <250ms and the mic transitions to listening.
   - **Web Audio Earcons:** Zero-latency synthesized frequency sweeps: `ack` (520Hz->880Hz rising wake chime), `action` (action start), `sleep` (780Hz->360Hz descending relaxation chime), `error` (warning tone).
   - **Speech Sanitizer (`sanitize_for_speech`):** Summarizes and suppresses raw URLs (speaks "the link on screen"), strips markdown/emojis, replaces code blocks with "I've displayed the code on screen", and cleanly truncates long replies (>400 chars).

### B. Device Command Channel & Remote Execution
- **Protocol:** Authenticated WebSocket (`/ws/device`) between Cloud Backend and Windows Companion Agent. Both `/ws/device` and `/api/*` enforce the server-side verification gate (`gate_state == 'active'`, closing gated agents with close code `4003` and `verification_required` / `approval_required` and waking automatically on `AIVA_VERIFICATION_UNLOCKED`).
- **Security & Capability Allowlist:**
  - `open_apps_urls`: Launch Windows executables, URLs, YouTube/Google compound search queries.
  - `media_volume`: Control volume levels (`0-100`), mute, play/pause, media track skipping.
  - `keyboard_input`: Type text strings, send virtual hotkeys (`Ctrl+C`, `Alt+Tab`, etc.).
  - `screenshot`: Capture desktop display for Gemini Vision analysis.
  - `power_control`: Lock screen, sleep, shutdown (destructive actions require explicit voice confirmation: *"Say 'confirm' to shut down"*).
  - `file_access`: Open local files/folders.

### C. Agent Core, Tiered Routing & Multi-Agent DAG Planning (`core/agent/`)
- **Feature-Flagged Agent Loop (`AIVA_AGENT_CORE`, default `0`, cutover `1` on Oracle VM):** When enabled (`docs/AGENT_ARCHITECTURE.md`), `TieredRouter` (`RULE_COUNT = 9`, `<10ms`) separates deterministic single-step actions (`Tier 0`) from fast Groq tool-calling (`Tier 1`) and deeper multi-step/research synthesis (`Tier 2`) orchestrated by `AgentOrchestrator` (`MAX_STEPS = 6`) over `LLMGateway`.
- **Typed Tools & Risk Policy (`core/agent/tools/`, `core/agent/policy.py`):** Wraps capabilities plus `whatsapp_inbox_status`, `whatsapp_read_messages`, `whatsapp_contact_preference`, `whatsapp_read`, `whatsapp_search`, `recall`, `create_reminder`, and `escalate` with JSON Schema validation, per-channel allowlists, `turn_origin == "user_explicit"` enforcement on private third-party inbox tools, `'say confirm'` / untrusted-taint confirmation gates, and `<tool_result untrusted="true">` fencing.
- **Streaming & Observability:** `/api/chat/stream` emits SSE stages, deltas, sentence-chunked Edge TTS audio (`pop_complete_sentences`), and normalized sources rendered with locally vendored `marked` + `DOMPurify`. Non-PII aggregates populate `/api/telemetry` and the HUD Engine Stats grid (`T0/T1/T2`, p50/p95, success rate).
- **Legacy Fallback (`AIVA_AGENT_CORE=0`):** `IntentClassifierService`, `PlannerAgent`, and `WorkflowEngine` remain intact with push delivery to `/ws/hud` and originating channels (`OutcomeReporterService`).
- **Specialized Agents:** `ResearchAgent` (awaited Tavily search + freshness TTL + source citations), `BrowserAgent` (Selenium + DOM observer), `ObserverAgent`, `MemoryAgent`.

### D. Unified Jarvis-Style Memory Engine (`core/memory/`)
- **Single Facade (`MemoryEngine`):** Replaces all legacy memory services behind one strict per-`user_id` isolated SQLite + numpy subsystem (`docs/MEMORY_SYSTEM.md`).
- **Storage & Schema (`core/memory/store.py`):** SQLite tables `memories`, `memories_fts` (FTS5), `memory_embeddings` (`float32` BLOB + numpy cosine similarity), content-hash `embedding_cache`, `entities` (linked to `whatsapp_contacts` without phone duplication), `entity_links`, `reminders`, `memory_audit` (no content logged), and `memory_migrations`.
- **Lifecycle Pipeline:**
  1. **Capture:** Voice, HUD, and WhatsApp turns append to a token-capped rolling working session buffer (`core/memory/extractor.py`). Explicit `remember` / `remind me` commands run deterministically in the hot path.
  2. **Extract (Async):** Background batch worker calls the multi-LLM failover pool with strict JSON validation, requeues on rate limit with exponential backoff, and filters sensitive secrets.
  3. **Consolidate:** Resolves conflicting semantic facts (`superseded_by` history preservation), deduplicates near-identical records, rolls idle sessions into episodic summaries, decays unaccessed low-value items, expires timed `knowledge`, and backfills missing embeddings (`core/memory/consolidator.py`).
  4. **Retrieve & Inject:** Hybrid search merges FTS5 + entity match + Gemini embeddings (`300 ms` timeout, keyword fallback) via RRF + composite scoring (`0.5*rel + 0.2*recency + 0.2*imp + 0.1*usage`), fenced in `<memory>` (`core/memory/retriever.py`, `injector.py`).
- **Proactive Scheduler (`core/memory/scheduler.py`):** 30 s tick loop delivers due reminders and opt-in morning briefings via WhatsApp, Web HUD WebSocket (`/ws/hud`), or voice, honoring quiet hours (`23:00`–`07:00` unless urgent), snooze, and recurrence.
- **Privacy & Controls:** Sensitive data filter (OTPs, passwords, API keys, Luhn cards, govt IDs), untrusted `web` provenance enforcement (`knowledge` + TTL only), prompt-injection neutralization, non-PII structured logging (`[Memory] stage=... duration_ms=... status=...`), REST CRUD/export/forget-all endpoints, and interactive HUD Memory panel.

### E. WhatsApp Channel Subsystem, Multi-User RBAC & Access Governance
- **Dedicated AIVA Account & Admin Identity:** Single `@whiskeysockets/baileys` (Node.js 20+ ESM, `services/whatsapp-bridge/`) bridge on `127.0.0.1:3001` strictly pinned to `AIVA_WHATSAPP_NUMBER` (rejects mismatch & ignores own messages). Each user links one personal E.164 number in `whatsapp_links`; the sole admin is identified by `AIVA_ADMIN_EMAIL`.
- **Bi-Directional Loopback & Media IPC:**
  - Bridge -> Python: `POST /internal/whatsapp/inbound` (`127.0.0.1` + constant-time `X-Bridge-Secret`).
  - Python -> Bridge: `POST /send`, `POST /media/download`, `POST /read` on `127.0.0.1:3001` (`X-Bridge-Secret`).
- **Deterministic Credential Challenge & Hot Config (`services/whatsapp_config.py`):**
  - Mtime-cached quoted `.env` loader (`AIVA_ACCESS_USERNAME`, `AIVA_ACCESS_PASSWORD` / `AIVA_ACCESS_PASSWORD_HASH`, `AIVA_ACCESS_EPOCH`) with constant-time compare, 2x lockout doubling, hourly challenge caps, batched 15 m admin notices, and epoch-bump re-auth. Zero challenge text reaches LLMs, logs, or inbox.
  - **Entry A (WhatsApp-first):** `aiva\` trigger -> username -> password -> explicit `users.name` prompt -> `ACTIVE` session. **Entry B (HUD-first):** `AIVA-XXXXXX` `wa.me` deep link (or outbound verify) -> admin exemption vs credential challenge -> live WS unlock (`verification_unlocked`).
- **Server-Side Gate & Non-Admin Guardrails:** Central Flask `403` middleware (`1.5s` TTL cache) + `/ws/device` enforce `gate_state == 'active'` (`number_pending`, `credential_pending`, `approval_pending`, `blocked`, `revoked`). Non-admins are bounded by `AIVA_USER_DAILY_MESSAGE_CAP`, `AIVA_USER_CAN_SEND_WHATSAPP=false`, and `WHATSAPP_ALLOW_POWER_CONTROL=false` (with 60s `CONFIRM` when enabled). Admin actions bump `token_version`, revoke WS/JWT sessions immediately, and write PII-free `admin_audit_logs`.
- **Sessions, Verbatim Sends & Metadata-Separated Inbox (`services/whatsapp_notifier.py`):**
  - Persistent `INACTIVE`/`ACTIVE` session machine (`aiva\`/`stop\`, `WHATSAPP_SESSION_IDLE_MINUTES`, scoped `reply_window_scope`). `send_to_contact` sends verbatim text with `"Sent to <Name>: <text>"` confirmations.
  - Unknown senders drop message text & increment anonymized counters. Saved contact 1:1 inbound captures into `whatsapp_inbox` with 120s debounced/quiet-hours metadata alerts (`priority` bypass, `notify_inbound` mute). `whatsapp_read_messages` requires explicit user intent, lazily transcribes voice via Whisper with temp-file wipe, posts `/read` post-read, and fences content in `<whatsapp_message untrusted="true">` (`untrusted_taint`).

---

## 4. Repository Directory Structure

```text
aiva/
├── aiva_server.py                      # Flask REST & WebSocket backend (Oracle Cloud VM)
├── main.py                             # CLI terminal entry point
├── config/
│   ├── secrets.json                    # API keys (Groq, Gemini, Mistral, Tavily)
│   ├── windows_apps.json               # Windows executable lookup table
│   └── linux_apps.json                 # Linux application table
├── core/
│   ├── runtime/ (kernel.py, event_bus.py, state_manager.py)
│   ├── agent/ (orchestrator.py, router.py, policy.py, llm_gateway.py, shaper.py, telemetry.py, tools/)
│   ├── memory/ (models.py, safety.py, store.py, retriever.py, injector.py, extractor.py, consolidator.py, scheduler.py, engine.py)
│   ├── agents/ (planner_agent.py, research_agent.py, browser_agent.py, memory_agent.py)
│   ├── planning/ (workflow_engine.py, workflow_graph.py)
│   ├── execution/ (action_dispatcher.py, capability_registry.py)
│   └── interaction/ (chat_runtime.py, input_handler.py)
├── deploy/                             # Systemd service deployment units
│   └── aiva-whatsapp.service           # Dedicated unit for Baileys Node bridge
├── docs/                               # Architecture documents & reports
│   ├── AGENT_ARCHITECTURE.md           # Phase 1 Agent Core, routing, tools, feature flag & eval guide
│   ├── AIVA_SYSTEM_CONTEXT.md          # This living specification file
│   ├── MEMORY_SYSTEM.md                # Unified memory architecture, schema & APIs
│   ├── VOICE_AUDIT.md                  # Voice pipeline latency & problem audit
│   ├── VOICE_FIXES.md                  # Log of verified voice fixes
│   └── WHATSAPP_SETUP.md               # Guide for WhatsApp bridge install & pairing
├── services/
│   ├── whatsapp-bridge/                # Node.js 20+ ESM Baileys linked device bridge
│   │   ├── package.json
│   │   ├── index.js                    # HTTP listener (127.0.0.1:3001), number guard, media & read receipts
│   │   ├── pairing.js                  # Baileys QR/code pairing state machine
│   │   └── pair.js                     # Terminal pairing code & QR utility
│   ├── whatsapp_config.py              # Mtime-cached quoted .env loader, password hash verify, triggers
│   ├── whatsapp_notifier.py            # Metadata-separated 1:1 inbox capture, debounce, quiet hours & purge
│   ├── whatsapp_service.py             # Entry A/B challenge, RBAC sessions, contacts, admin cmds & Whisper read
│   ├── voice_service.py                # Edge TTS, Groq Whisper STT, speech sanitizer
│   ├── device_channel.py               # Cloud WebSocket server for device agents
│   ├── device_manager.py               # Pairing, heartbeat, device revocation
│   ├── auth_service.py                 # OTP generation, JWT creation/refresh
│   └── database_service.py             # SQLite schemas & queries
├── ui/                                 # Web HUD (rendered directly or embedded in iframe)
│   ├── index.html                      # Central Command Center, Neural Orb, Memory & WhatsApp UI
│   ├── app.js                          # HUD state sync, Memory/Reminder controls, /ws/hud client
│   ├── voice-manager.js                # Single VoiceStateMachine, VAD, earcons, follow-up
│   ├── tokens.css                      # Indigo-to-cyan design system variables
│   ├── style.css                       # HUD styles, 60fps Neural Orb & Memory panel styles
│   └── vendor/                         # Vendored marked.min.js & purify.min.js (offline-safe)
├── scripts/
│   └── run_eval.py                     # Golden evaluation runner (stub & --live modes)
├── windows-agent/                      # Windows Companion Electron Desktop App
│   ├── package.json                    # Electron 33 packaging & NSIS builder
│   ├── src/main/
│   │   ├── index.js                    # Electron main process, tray, global PTT shortcut
│   │   ├── wake-word-engine.js         # Native offline Windows SAPI wake word engine
│   │   ├── ws-client.js                # Persistent WebSocket client to cloud backend
│   │   ├── executor.js                 # Local PowerShell / OS action runner
│   │   ├── permissions.js              # Local capability toggle manager
│   │   └── secure-store.js             # DPAPI encrypted credential store
│   └── src/renderer/
│       ├── main.html                   # Desktop app frame with embedded HUD iframe
│       └── renderer.js                 # Bridge between native agent and web HUD iframe
└── tests/                              # Regression, integration & eval suite (eval/golden_commands.yaml)
```

---

## 5. Design System & UI Conventions

- **Palette:** Dark slate background (`#0B0F17`, `#111827`), primary accents in cyan (`#06B6D4`, `#22D3EE`) and indigo (`#4F46E5`, `#6366F1`).
- **Typography:** Inter / system sans-serif for UI, JetBrains Mono for telemetry/tokens/code.
- **Central Neural Orb (`#neural-core`):** 60fps CSS gradient orb with dynamic reactive RMS scaling:
  - `idle`: Slow breathing cyan-indigo pulse.
  - `listening`: Reacts dynamically to microphone RMS energy.
  - `processing`: Rotating gradient spinner.
  - `speaking`: Rhythmic expansion reacting to audio output.
  - `follow_up`: Soft cyan countdown ring indicating open follow-up mic.
  - `muted`: Dimmed gray slash.
  - `error`: Soft red warning pulse with automatic 2.2s recovery.

---

## 6. Guidelines for LLM Prompt & Code Generation

When instructing an LLM (Claude, etc.) to generate new features, bug fixes, or user prompts for AIVA:
1. **Preserve System Invariants:** Never compromise per-user data isolation, JWT authentication, device channel allowlists, or break existing regression tests (`python -m unittest discover tests`).
2. **Respect the Hybrid Split:** The cloud backend VM runs headless in Linux with **no audio hardware**. All audio capture, wake-word detection, and speaker playback happen exclusively on the client side (`ui/voice-manager.js` or `windows-agent`).
3. **Strict Design Tokens:** Use variables defined in `ui/tokens.css` (e.g. `var(--color-primary)`, `var(--bg-card)`); never hardcode raw arbitrary hex/RGB colors.
4. **Single Source of Truth:** Any voice state change must transition through `AivaVoice.stateMachine.transition()`; avoid scattered boolean flags or unmanaged timer loops.
5. **Privacy in Logs:** Server-side logging must use structured, non-PII format (`[VoicePipeline] stage=... duration_ms=... status=...`) and must never log raw audio or private transcript content.

