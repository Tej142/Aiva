# AIVA Unified Memory System (`core/memory/`)

> **Status:** Production Architecture  
> **Replaces:** `UserMemoryService`, `ConversationMemoryService`, `KnowledgeService`, `MemoryService`, `RecallService`, `MemoryExtractionService`, `PassiveMemoryService`

---

## 1. Architecture & Package Layout

The memory subsystem lives in `core/memory/` behind a single facade, [`MemoryEngine`](file:///d:/aiva/core/memory/engine.py), ensuring strict `user_id` tenant isolation, non-PII structured logging (`[Memory] stage=... duration_ms=... status=...`), and bounded hot-path latency for real-time voice, HUD, and WhatsApp conversations.

| Module | Role |
|---|---|
| [`models.py`](file:///d:/aiva/core/memory/models.py) | Typed dataclasses (`MemoryItem`, `EntityItem`, `EntityLink`, `ReminderItem`, `WorkingTurn`, `MemorySettings`) and enums (`MemoryType`, `MemorySource`, `Channel`, `MemoryStatus`, `EntityKind`, `ReminderStatus`). |
| [`safety.py`](file:///d:/aiva/core/memory/safety.py) | Non-PII logger (`log_memory`), sensitive-data scanner (OTPs, passwords, API keys, Luhn cards, government IDs), source provenance validation, prompt-injection sanitizer, and `<memory>` data fencing. |
| [`store.py`](file:///d:/aiva/core/memory/store.py) | Thread-safe SQLite CRUD, FTS5 full-text index & sync triggers, `float32` BLOB vector storage, content-hash `embedding_cache`, entity & WhatsApp contact link reuse, reminders, audit logs, and versioned migrations (`v1_backfill_legacy`, `v2_drop_legacy_tables`). |
| [`retriever.py`](file:///d:/aiva/core/memory/retriever.py) | `EmbeddingProvider` (Gemini `gemini-embedding-001` failover pool + 300 ms timeout + keyword fallback) and `HybridRetriever` (FTS5 + entity/WhatsApp match + `numpy` cosine similarity merged with Reciprocal Rank Fusion and composite ranking). |
| [`injector.py`](file:///d:/aiva/core/memory/injector.py) | Token-budgeted prompt builder combining an always-on Core Profile (~150 tokens) and up to 5 relevant memories inside `<memory>` XML fencing. |
| [`extractor.py`](file:///d:/aiva/core/memory/extractor.py) | Per-session token-capped rolling working buffer, hot-path explicit `remember`/`remind me` commands, and asynchronous daemon batch extractor calling the multi-LLM failover pool with strict JSON validation and exponential-backoff rate-limit requeue. |
| [`consolidator.py`](file:///d:/aiva/core/memory/consolidator.py) | Idle/periodic maintenance: semantic conflict supersession (`superseded_by`), Jaccard deduplication, working-turn episodic summarization, unused importance decay, `expires_at` expiry, and lazy embedding backfill. |
| [`scheduler.py`](file:///d:/aiva/core/memory/scheduler.py) | Background loop checking due prospective reminders every 30 s, honoring quiet hours (default `23:00`–`07:00` unless urgent), recurrence, snooze, opt-in morning briefings, and multi-channel delivery (`whatsapp`, `hud`, `voice`). |
| [`engine.py`](file:///d:/aiva/core/memory/engine.py) | Unified `MemoryEngine` facade consumed by `InputHandler`, `chat_runtime`, `MemoryAgent`, `ResearchAgent`, `PlannerAgent`, `WhatsappService`, `AivaKernel`, and `aiva_server`. |

---

## 2. SQLite Schema (`storage/aiva.db`)

All tables are strictly partitioned by `user_id` (`FOREIGN KEY` references `users(id) ON DELETE CASCADE` where applicable):

- **`memories`**: `(id, user_id, type[working|episodic|semantic|procedural|prospective|knowledge], content, summary, entities_json, importance, confidence, source[user_said|inferred|web|system], channel[voice|hud|whatsapp], created_at, last_accessed, access_count, valid_from, superseded_by, expires_at, status[active|superseded|forgotten])`
- **`memories_fts`**: FTS5 virtual table over `content`, `summary`, and `entities` synchronized cleanly on insert, update, and purge.
- **`memory_embeddings`**: `(memory_id, user_id, vector BLOB, model, dim)` storing `numpy float32` normalized vectors (no external SQLite vector extensions required).
- **`embedding_cache`**: `(content_hash, model, dim, vector BLOB, updated_at)` keyed by SHA-256 content hash + model to avoid redundant Gemini embedding calls.
- **`entities`**: `(id, user_id, name, kind[person|device|project|place|org|other], aliases_json, attributes_json, whatsapp_contact_id)` linked by reference to `whatsapp_contacts(id)` without duplicating phone numbers.
- **`entity_links`**: `(user_id, from_id, to_id, relation)` representing person/project/device relationships.
- **`reminders`**: `(id, user_id, text, due_at, recurrence, channel, status[pending|sent|done|snoozed|cancelled], created_from_memory_id, urgent, updated_at)`.
- **`memory_audit`**: `(id, user_id, action, memory_id, created_at)` — stores zero memory text or PII.
- **`memory_migrations`**: `(version, applied_at)` tracking one-time schema transitions (`v1_backfill_legacy`, `v2_drop_legacy_tables`).

---

## 3. Memory Lifecycle & Latency Budgets

1. **Capture (Hot Path):** Every turn from Voice, Web HUD, or WhatsApp calls `MemoryEngine.observe(user_id, turn, channel)`. Working turns exceeding the session token cap roll oldest turns into a compact rolling summary.
2. **Async Extract (Background Thread):** Every batch (or ~30 s idle), `MemoryExtractor` formats recent turns into a strict-JSON extraction prompt (`semantic`, `episodic`, `procedural`, `prospective`, `entities`, `relations`). Provider rate limits (`429`) requeue the job with exponential backoff into `_deferred` instead of dropping silently. Explicit commands (`remember that...`, `remind me...`, `forget ...`, `snooze ...`, `what do you know about ...`) execute immediately in the hot path; commitments without a time prompt the user **once** instead of guessing.
3. **Consolidate:** Resolves conflicting semantic facts (`superseded_by` chain preserves history while hiding stale entries from active retrieval), deduplicates near-duplicates (`Jaccard >= 0.85`), summarizes idle working sessions into `episodic` memories, decays unaccessed low-importance items after 14 days, deletes expired `knowledge` rows (`expires_at`), and backfills missing embeddings.
4. **Retrieve:** Runs FTS5 keyword search + entity/WhatsApp contact match + optional Gemini embedding similarity (`timeout_s = 0.300`) in parallel / failover, merges lists via Reciprocal Rank Fusion (`k = 60`), then ranks with:
   $$\text{Score} = 0.5 \cdot \text{relevance} + 0.2 \cdot \text{recency\_decay} + 0.2 \cdot \text{importance} + 0.1 \cdot \text{usage}$$
   and updates `access_count` and `last_accessed` on selected memories.
5. **Inject:** `MemoryInjector` compiles a small always-on Core Profile (`~150` tokens) plus up to `5` query-relevant memories inside a total `450`-token cap, fenced in `<memory role="user_data" untrusted="true">`.
6. **Hot-Path Latency:** Voice/HUD context assembly completes within **<= ~60 ms** on keyword+entity paths and cleanly degrades within the **300 ms** timeout cap when remote embeddings time out or are rate-limited.

---

## 4. Safety, Privacy & Provenance

- **Zero Content Logging:** `log_memory()` records only `[Memory] stage=... duration_ms=... status=...` plus safe counters/type codes.
- **Sensitive Data Blocking:** `contains_sensitive_data()` rejects OTPs, passwords/PINs, API tokens (`AIza...`, `sk-...`, `gsk_...`, `ghp_...`, `tvly-...`, JWT/Bearer), Luhn-validated or labeled card numbers/CVVs, and government IDs (`SSN`, `Aadhaar`, `PAN`) before entering working buffers or long-term storage.
- **Provenance Enforcement:** Untrusted web research (`source='web'`) is permitted **only** with `type='knowledge'` and a finite `expires_at` TTL; untrusted sources cannot write `semantic` or `procedural` memories.
- **User Controls:** Settings toggles (`memory_enabled`, `learning_paused`, `morning_briefing_enabled`, `quiet_hours_start/end`, `preferred_reminder_channel`) work via REST API, HUD panel, and natural language commands. `forget_memory()` and `forget_all()` purge `memories`, `memories_fts`, and `memory_embeddings` completely.

---

## 5. REST & WebSocket Endpoints

All endpoints require JWT (`@require_auth`) and enforce `uid = user["id"]`:

- `GET /api/memory?q=...&type=...` — Search/filter active memories + return entities, reminders, settings, and task patterns.
- `PATCH /api/memory/<id>` / `DELETE /api/memory/<id>` — Edit or permanently forget a single memory.
- `POST /api/memory/forget-all` (`{"confirm": true}`) — Permanently erase all memories, entities, links, and reminders for the account.
- `GET /api/memory/export` — Download full JSON archive (`aiva-memory-export.json`).
- `GET /api/memory/settings` / `PATCH /api/memory/settings` — Read/update memory privacy, quiet hours, preferred channel, and morning briefing preferences.
- `GET /api/reminders` / `POST /api/reminders` — List or create reminders (ISO or natural language due times).
- `PATCH /api/reminders/<id>` / `DELETE /api/reminders/<id>` — Update status, snooze (`{"snooze_seconds": 600}`), or cancel.
- `WS /ws/hud?token=<jwt>` — Streams real-time proactive reminder and morning briefing notifications to open HUD clients.

