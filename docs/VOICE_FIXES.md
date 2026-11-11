# AIVA Voice Pipeline Permanent Fixes Log (Phase 1)

This document records the architectural root causes, permanent solutions, regression tests, and recurrence detection strategies for every issue identified in `docs/VOICE_AUDIT.md`.

---

## Fix Summary Matrix

| ID | Issue | Root Cause | Layer | Fix Description | Test Name | Recurrence Detection |
|---|---|---|---|---|---|---|
| **FIX-01** | Headless server audio crash | Cloud Linux VM has no soundcards / ALSA sinks | Backend (`voice_service.py`) | Added headless safety check around `sd.play()`; audio bytes generated cleanly without attempting hardware output | `test_headless_audio_safety` | Server log warning `Local audio playback skipped` |
| **FIX-02** | Scattered boolean state machine | Voice state split between `isRecording`, `isVoiceMuted`, `silenceTimer`, etc. | Client (`ui/voice-manager.js`) | Replaced with unified single-source `VoiceStateMachine` (`IDLE`, `LISTENING`, `PROCESSING`, `SPEAKING`, `FOLLOW_UP`, `MUTED`, `ERROR`) | `test_voice_state_machine_valid_lifecycle`, `test_voice_state_machine_rejects_illegal_transitions` | Console log `invalid_transition` emitted |
| **FIX-03** | Audio stream & handle leaks | Mic tracks only closed in `onstop`; unclosed contexts on error/unload | Client (`ui/voice-manager.js`) | Centralized `AudioSessionManager` with `dispose()` and guaranteed cleanup on stop, error, logout, and `beforeunload` | Integration verification | Browser active mic indicator remaining on after stop |
| **FIX-04** | Missing VAD / 12s silence freeze | Hardcoded 12-second timeout without energy/RMS detection | Client (`ui/voice-manager.js`) | Implemented Web Audio `AnalyserNode` RMS energy calculator with 800ms natural pause tolerance and 300ms min speech guard | VAD unit test & browser listener | Console log `[VoicePipeline] stage=vad event=speech_ended` |
| **FIX-05** | No barge-in / interruption | Mic disabled during audio playback; manual mouse click required | Client (`ui/voice-manager.js`) | `AudioSessionManager.bargeIn()` halts `Audio` playback immediately (<250ms) and transitions directly to `LISTENING` | `test_voice_state_machine_barge_in_transition` | User interruption latency benchmark |
| **FIX-06** | Missing follow-up window | UI reset to idle immediately after TTS playback completed | Client (`ui/voice-manager.js`) | Implemented `FOLLOW_UP` state (6-second conversational window) with countdown interval and auto-sleep | `test_voice_state_machine_valid_lifecycle` | Multi-turn speech without wake word |
| **FIX-07** | Echo / acoustic self-trigger | Unconstrained `getUserMedia` picking up laptop speakers | Client (`ui/voice-manager.js`) | Enforced `{ audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true } }` | Soak audio test | Whisper hallucination rate during speaker playback |
| **FIX-08** | Verbose persona reads raw code & URLs | Inadequate regex sanitizer reading full URLs and code blocks | Backend (`services/voice_service.py`) | Comprehensive `sanitize_for_speech`: replaces code blocks with screen announcement, URLs with "link on screen", strips markdown & emoticons, truncates >400 chars | `test_sanitize_code_blocks`, `test_sanitize_urls_and_markdown_links`, `test_sanitize_markdown_symbols_and_emojis`, `test_sanitize_excessive_length_summarization` | TTS containing literal `http://` or markdown syntax |
| **FIX-09** | Missing typed error model | Errors swallowed or shown as generic toasts | Client (`ui/voice-manager.js`) | Typed error model (`MIC_DENIED`, `NO_DEVICE`, `MIC_UNPLUGGED`, `STT_FAILED`, `TTS_FAILED`, `NETWORK`, `TIMEOUT`, `INSECURE_CONTEXT`) with human guidance | `test_typed_error_model`, `test_voice_state_machine_error_recovery` | Unhandled error toast or red error banner |
| **FIX-10** | Unstructured logging | Inconsistent console prints with raw transcripts | Full Stack | Structured logging `[VoicePipeline] stage=... duration_ms=... status=...` without PII or raw transcripts | `test_voice_pipeline.py` | Log audit for PII or transcript leaks |
| **FIX-11** | Follow-up window visual-only (Mic not active) | `enterFollowUpWindow` set `VoiceState.FOLLOW_UP` and a countdown timer without acquiring the microphone | Client (`ui/voice-manager.js`) | `enterFollowUpWindow` now invokes `startListening({ isFollowUp: true })` so microphone is actively recording during the 6s window with live VAD turn-taking | Live conversation verification | Conversation continuing across multi-turn queries |
| **FIX-12** | Red Orb stuck on silence / empty Whisper STT | Empty transcripts from Whisper treated as fatal `STT_FAILED`; `VoiceStateMachine` lacked auto-reset timer | Client (`ui/voice-manager.js`) | Empty STT returns softly to `IDLE` with gentle sleep earcon; `setError` automatically recovers to `IDLE` after 2.2s | Client test & manual verification | Central Orb returning to indigo-cyan breathing state |
| **FIX-13** | Wake word server-client mismatch | WakeWordService ran on cloud VM with no physical mic | Client (`ui/voice-manager.js`) | Integrated on-device keyword spotter for "Hey AIVA" and "AIVA" in Chromium/Electron; plays `ack` earcon immediately | Wake word utterance test | Rising wake chime sounding on "Hey AIVA" |
| **FIX-14** | Mode buttons disconnected from client voice manager | Clicking "Adaptive", "Wake Word", "All Voice" only posted to server | Client (`ui/app.js`) | Linked HUD mode toggles directly to `window.AivaVoice.setMode()`, updating client wake-word and continuous listening policies | UI Mode toggle verification | Mode badge matching runtime behavior |

---

## Detailed Root Cause Analysis & Resolutions

### 1. Unified State Machine (`VoiceStateMachine`)
- **File:** [`ui/voice-manager.js`](file:///d:/aiva/ui/voice-manager.js)
- **Design:** Implements strict directed graph transitions. Any illegal transition is trapped and logged. State changes dispatch events to subscribers.
- **Benefits:** Eliminates race conditions between button clicks, audio playback completion, and network timeouts.

### 2. Centralized Audio Session Manager (`AudioSessionManager`)
- **File:** [`ui/voice-manager.js`](file:///d:/aiva/ui/voice-manager.js)
- **Design:** Encapsulates the entire Web Audio API lifecycle. Tracks `AudioContext`, `MediaStream`, `AnalyserNode`, and `MediaRecorder`.
- **Cleanup Guarantee:** Hooks into `window.addEventListener('beforeunload', ...)` and `navigator.mediaDevices.ondevicechange`. When the microphone is unplugged or the window is closed, all tracks are guaranteed stopped.

### 3. Real-time Voice Activity Detection (VAD)
- **File:** [`ui/voice-manager.js`](file:///d:/aiva/ui/voice-manager.js)
- **Design:** Analyzes time-domain PCM samples via `AnalyserNode.getByteTimeDomainData` at 60fps. Calculates Root Mean Square (RMS) energy.
- **Tuning:**
  - Speech threshold: `RMS > 0.018`
  - Silence pause tolerance: `800 ms`
  - Minimum speech duration: `300 ms` (rejects accidental clicks/microphone thumps)
  - Hard stop safety guard: `15,000 ms`

### 4. Text-for-Speech Sanitization (`sanitize_for_speech`)
- **File:** [`services/voice_service.py`](file:///d:/aiva/services/voice_service.py)
- **Design:**
  - Code blocks (` ```python ... ``` `) are extracted and replaced with `"I've displayed the code on your screen."`.
  - Bare URLs (`https://...`) are spoken as `"the link on screen"`.
  - Markdown links (`[AIVA Portal](https://...)`) speak `"AIVA Portal"`.
  - Markdown symbols (`*`, `_`, `#`, `~`, `|`) are stripped.
  - Length truncation at sentence boundaries with max 400 characters, appending `"I've placed the full details on your screen."`.

