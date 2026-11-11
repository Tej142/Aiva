# AIVA Phase 1 & Phase 2 Agent Architecture

> **Status:** Implemented behind `AIVA_AGENT_CORE` (live-ready cutover `AIVA_AGENT_CORE=1`; instant rollback `AIVA_AGENT_CORE=0`)  
> **Updated:** 2026-11-11

## 1. Overview & Request Lifecycle

Phase 1 upgrades AIVA from regex/pattern-heavy dispatch (`core/interaction/input_handler.py`) to a typed, policy-enforced, streaming-first **Agent Core** (`core/agent/`) while keeping backward compatibility behind the `AIVA_AGENT_CORE` feature flag. Phase 2 extends the registry and Baileys WhatsApp subsystem with private, FTS5-indexed **WhatsApp message reading & searching** (`whatsapp_read`, `whatsapp_search`).

```text
User Turn (HUD / Voice / WhatsApp)
   │
   ├─► Identity & Hot-Path Memory Guard (preferred_name, "call me X", "remember...", "remind me...")
   │
   ├─► TieredRouter (core/agent/router.py, <10 ms, RULE_COUNT = 9 <= 15)
   │      ├── Tier 0: Direct single-step reply or deterministic tool (time, volume, open app, lock, screenshot)
   │      ├── Tier 1: Fast tool-calling agent (Groq pool, 7 keys)
   │      └── Tier 2: Complex multi-step / web synthesis / browser automation (Gemini/Mistral pools, 5+5 keys)
   │
   ├─► AgentOrchestrator (core/agent/orchestrator.py, MAX_STEPS=6, PER_STEP=20s, TOTAL=55s)
   │      ├── Parallel execution for read/parallel-safe tools
   │      ├── Sequential execution + Risk Policy (core/agent/policy.py)
   │      ├── Untrusted fencing: <tool_result untrusted="true">
   │      └── Execution-derived telemetry (core/agent/telemetry.py)
   │
   └─► Response Shaper & Streaming (core/agent/shaper.py + /api/chat/stream)
          ├── Voice: <= 2 concise sentences + Edge TTS per-sentence audio chunks
          ├── HUD: Vendored GFM Markdown (marked + DOMPurify) + collapsible Sources drawer
          └── WhatsApp: Concise text formatted via format_for_whatsapp
```

---

## 2. Subsystems Implemented

### A. Agent Core (`core/agent/`)
- **`orchestrator.py` (`AgentOrchestrator`):** Runs the bounded tool loop (`MAX_STEPS = 6`, `PER_STEP_TIMEOUT_S = 20.0`, `TOTAL_TIMEOUT_S = 55.0`), executes `parallel_safe` tool batches concurrently via `asyncio.gather`, maintains per-`(user_id, session_id)` confirmation slots, compacts verbose tool outputs into `ref_id` working-memory references, and derives telemetry `tag` from actual tool calls.
- **`llm_gateway.py` (`LLMGateway`):** Unified async client over Groq (7 keys), Gemini (5 keys), and Mistral (5 keys) with `AsyncGroq(..., max_retries=0)` fast key rotation, 404 model fallback (`_is_model_unavailable_error`), `reasoning_effort="low"` on Tier 1 `openai/gpt-oss-20b` (`qwen/qwen3.8-27b` fallback), Tier 2 Gemini pools (`gemini-3-flash-preview`, `gemini-3.1-flash-lite`, `gemini-2.5-flash-lite`), Tier 2 Groq fallback (`openai/gpt-oss-120b`), native OpenAI-style tool calls + JSON fallback parser (`_extract_json_tool_calls`), and first-token/tool-call delta streaming.
- **`router.py` (`TieredRouter`):** Strictly capped (`TIER0_MAX_RULES = 15`, currently `RULE_COUNT = 9`) for sub-10ms deterministic single-step actions while excluding compound clauses (`and`, `then`) and ambiguous pronouns (`open that`, `open it`). Escalates deep research, multi-clause tasks (`>= 3`), and browser workflows directly to Tier 2.

### B. Typed Tool Registry & Risk Policy (`core/agent/tools/`, `core/agent/policy.py`)
- **`ToolRegistry` & `ToolSpec` (`core/agent/tools/registry.py`):** JSON Schema validation (`ToolValidationError`), per-channel allowlists (`hud`, `voice`, `whatsapp`), timeouts, and preflight resolution.
- **Built-in Tools (15 tools, `core/agent/tools/builtin.py`):** `open_apps_urls`, `media_volume`, `keyboard_input`, `screenshot`, `power_control` (`hud`/`voice` only), `file_access`, `web_research` (`untrusted_result=True`), `browser_actions` (`untrusted_result=True`), `whatsapp_lookup_contact`, `whatsapp_send` (resolves `second number` / `is_self`, checks ambiguity & recipient trust), `whatsapp_read` (`RISK_READ`, `untrusted_result=True`, unread/hours/contact filter, ambiguity disambiguation, masked numbers), `whatsapp_search` (`RISK_READ`, `untrusted_result=True`, FTS5 + `LIKE` search across saved & unsaved chats), `recall`, `create_reminder`, and `escalate`.
- **Phase 2 Private WhatsApp Archival & HUD Inbox:**
  - `DatabaseService` maintains per-`user_id` isolated `whatsapp_messages` + `whatsapp_messages_fts` (`wa_msg_ai`/`ad`/`au` triggers) with deduplication (`idx_wa_msg_user_waid`) and masked phone numbers.
  - `services/whatsapp-bridge/index.js` archives outbound phone messages (`fromMe`), history sync replays (`'append'` / `messaging-history.set`), and `@lid` resolved peer JIDs via loopback `POST /internal/whatsapp/archive` (`X-Bridge-Secret`). Saved-contact inbound messages directly to the AIVA number archive silently as `archived_contact_message`.
  - Authenticated HUD endpoints `GET /api/whatsapp/messages` and `POST /api/whatsapp/messages/read` power the **Recent WhatsApp Messages** search/filter card in `ui/index.html` (`tokens.css` variables only).
- **Policy & Untrusted Fencing (`core/agent/policy.py`):**
  - `read`: runs freely.
  - `low`: runs directly with concise confirmation/feedback (trusted WhatsApp contacts, app launches).
  - `confirm`: requires explicit `'confirm'` turn (`shutdown`, `restart`, `sleep`, `close_app`, non-trusted WhatsApp recipients) and rejects ambiguous or unknown contacts before any external dispatch.
  - `fence_tool_result(..., untrusted=True)`: strips prompt-injection directives (`sanitize_untrusted_text`) and fences third-party/web text in `<tool_result untrusted="true" provenance="data_only">`.

### C. Streaming HUD, Sentence-Chunked TTS & Markdown (`aiva_server.py`, `ui/`)
- **`POST /api/chat/stream`:** JWT-authenticated Server-Sent Events emitting `status` (`thinking`, `searching`, `reading sources`, `sending message`), token `delta`, sentence-chunked `audio_chunk` (Edge TTS MP3 per completed sentence via `pop_complete_sentences` + `VoiceService.generate_tts_audio`), and `final` (`response`, `sources`, `tier`, `tag`, `timings`).
- **HUD Markdown (`ui/index.html`, `ui/vendor/`, `ui/app.js`):** Locally vendored `purify.min.js` and `marked.min.js` (zero external CDNs), rendered with GFM tables, lists, code blocks, sanitized external links (`target="_blank" rel="noopener noreferrer"`), and collapsible `<details class="chat-sources">` drawers using only `ui/tokens.css` variables.
- **Audio Session Queue (`ui/voice-manager.js`):** `beginAudioStream`, `enqueueAudioChunk`, and `finishAudioStream` stream sequential chunks through `VoiceStateMachine`, preserve the 6s `FOLLOW_UP` window, play the `action` earcon on search/tool work, and cancel playback + SSE `AbortController` immediately on `bargeIn()`.

### D. Telemetry & Engine Stats Dashboard (`core/agent/telemetry.py`, `aiva_server.py`, `ui/`)
- Non-PII SQLite aggregate table `agent_telemetry` (never stores prompts, transcripts, or phone numbers) + structured `[Agent] stage=... duration_ms=... status=...` logging.
- `GET /api/telemetry` (`@require_auth`) and extended `GET /api/status` feed the HUD Engine Stats grid (**Tier Mix** `T0/T1/T2`, **Latency p50/p95**, **Success Rate**, plus Groq 7 / Gemini 5 / Mistral 5 pools).

---

## 3. Root-Cause Audits Resolved in Phase 1

1. **Greeting Bug (`"Hey Telugu"` instead of `"Charan"`):**
   - Caused by inferred language attributes polluting name memory slots and overriding `users.name`.
   - Fixed by schema/safety classification (`core/memory/safety.py`, `core/memory/extractor.py`), database migration (`reclassify_invalid_name_memory`, superseding invalid name rows without deletion and scrubbing vocative working turns), explicit `"call me <name>"` `preferred_name` precedence, and pinned `<identity authoritative="true">` XML in prompts (`core/memory/injector.py`, `core/agent/orchestrator.py`).
2. **Stale Research Cache, `"URLs accessed on 7 Nov 2026"`, & Raw Markdown:**
   - Fixed unawaited/mismatched Tavily response handling in `core/agents/research_agent.py`, added relevance (`AIVA_KNOWLEDGE_MIN_RELEVANCE=0.45`) + token overlap + TTL freshness enforcement (`AIVA_KNOWLEDGE_CACHE_TTL_HOURS=24`), removed hallucinated access dates from `services/mistral_service.py` `summarize_research`, compacted `working` memory outputs with `ref_id`, pushed background `workflow_result` completions over `/ws/hud` & originating channels, and replaced regex markdown parsing with vendored `marked` + `DOMPurify`.
3. **`user_task_patterns` Audit (`services/task_pattern_service.py`):**
   - Inspection showed polluted patterns storing casual greetings, hardcoded phone numbers, and unparameterized WhatsApp sends. Rather than dropping the table, `is_clean_pattern()` filters out noisy/hardcoded entries and `get_agent_hints()` surfaces only clean, parameterized workflow hints as optional prompt context.

---

## 4. Feature Flag Rollout & Instant Rollback

The Phase 1 orchestrator is gated by `AIVA_AGENT_CORE`:
- **Default (`0`):** `InputHandler.process_query`, `/api/chat`, and `WhatsAppService` use the legacy path while still benefiting from Identity Pinning, fixed `ResearchAgent`, `/api/chat/stream` SSE/TTS/Markdown, and telemetry capture.
- **Enable Agent Core on Oracle VM:**
  ```bash
  # On opc@140.245.254.83 inside /path/to/aiva (.env)
  AIVA_AGENT_CORE=1
  sudo systemctl restart aiva
  ```
- **Instant Zero-Code Rollback:**
  ```bash
  # Revert AIVA_AGENT_CORE=0 in .env (or remove it)
  AIVA_AGENT_CORE=0
  sudo systemctl restart aiva
  ```

---

## 5. Golden Evaluation Suite

- Dataset: `tests/eval/golden_commands.yaml` (`129` labeled commands across 12 categories: device control, trusted WhatsApp, ambiguous/unknown/non-trusted WhatsApp, `search + send`, identity/greeting regression, conversation/memory/reminders, research + sources, multi-step/browser/docs, Telugu/Tanglish bilingual, vague clarification, adversarial injection/channel-isolation safety, and Phase 2 WhatsApp read & search).
- Runner: `scripts/run_eval.py`
  ```bash
  # Fast deterministic CI mode (stubbed LLM, real Orchestrator + Policy + Memory + SQLite)
  python scripts/run_eval.py --min-accuracy 0.90

  # Opt-in live LLM gateway benchmark (with stratified sampling & rate-limit pacing)
  python scripts/run_eval.py --live --limit 25 --delay-ms 350 --min-accuracy 0.85
  ```

