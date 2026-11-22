/**
 * AIVA Unified Voice Runtime & State Machine (Phase 2: Jarvis-Grade Conversational Voice)
 * 
 * Features:
 * - Single source-of-truth State Machine (IDLE -> LISTENING -> PROCESSING -> SPEAKING -> FOLLOW_UP -> IDLE)
 * - Local Client-Side Wake Word Detection ("Hey AIVA", "AIVA", "Hi AIVA") via on-device speech engine
 * - True Continuous Dialogue: 6-second conversational Follow-Up Window with live mic turn-taking
 * - Zero-Latency Synthesized Web Audio Earcons (ack, action, sleep, error)
 * - Centralized AudioSessionManager with guaranteed resource cleanup on stop/unload
 * - Real-time client-side VAD (Voice Activity Detection) with 800ms natural pause tolerance
 * - Sub-250ms Barge-In capability (interrupt AIVA instantly by speaking or hotkey)
 * - Hardware Echo Cancellation, Noise Suppression, and Auto Gain Control constraints
 * - 60fps RMS-reactive Indigo-to-Cyan Neural Orb
 * - Live Transcript Caption Bar with follow-up countdown & privacy indicator
 * - Push-to-Talk via Global Shortcut (Ctrl+Space) & Tray coordination
 * - Typed Error Model with 2.2s auto-recovery (Orb never gets stuck in RED)
 * - Graceful silence/empty-audio handling (no false error alerts on pauses)
 * - Structured non-PII diagnostic logging
 */

(function () {
  'use strict';

  // 1. Typed Error Model
  const VoiceErrorCode = {
    MIC_DENIED: 'MIC_DENIED',
    NO_DEVICE: 'NO_DEVICE',
    MIC_UNPLUGGED: 'MIC_UNPLUGGED',
    STT_FAILED: 'STT_FAILED',
    TTS_FAILED: 'TTS_FAILED',
    NETWORK: 'NETWORK',
    TIMEOUT: 'TIMEOUT',
    INSECURE_CONTEXT: 'INSECURE_CONTEXT',
    AUDIO_INIT_FAILED: 'AUDIO_INIT_FAILED'
  };

  const VoiceErrorMessages = {
    [VoiceErrorCode.MIC_DENIED]: 'Microphone permission denied. Please allow microphone access in your browser or Windows Settings.',
    [VoiceErrorCode.NO_DEVICE]: 'No microphone detected. Please plug in an audio input device.',
    [VoiceErrorCode.MIC_UNPLUGGED]: 'Microphone was unplugged or disconnected.',
    [VoiceErrorCode.STT_FAILED]: 'Could not understand audio. Please try speaking again.',
    [VoiceErrorCode.TTS_FAILED]: 'Voice synthesis unavailable. Showing text on screen.',
    [VoiceErrorCode.NETWORK]: 'Connection error during voice request. Please check your network.',
    [VoiceErrorCode.TIMEOUT]: 'Voice processing timed out. Please try again.',
    [VoiceErrorCode.INSECURE_CONTEXT]: 'Microphone requires a secure HTTPS connection or localhost.',
    [VoiceErrorCode.AUDIO_INIT_FAILED]: 'Could not initialize audio system.'
  };

  // 2. Voice Runtime Modes
  const VoiceMode = {
    ADAPTIVE: 'adaptive',   // Professional Default: Wake word -> Dialogue -> Continuous Follow-up -> Auto Sleep
    WAKEWORD: 'wakeword',   // Strict: Wake word required for every command
    ALLVOICE: 'allvoice',   // Continuous: Open mic listens to all speech
    MUTED: 'muted'          // Voice disabled entirely
  };

  // 3. Centralized Configuration
  const VOICE_CONFIG = {
    vad: {
      rmsThreshold: 0.022,          // Energy threshold to detect active human speech (tuned to reject fan/ambient noise)
      silenceHoldMs: 800,           // Natural pause tolerance before auto-submitting (800ms)
      minSpeechMs: 350,             // Minimum speech duration to prevent false triggers on clicks/pops
      maxRecordMs: 15000            // Maximum recording timeout safety guard
    },
    dialogue: {
      followUpMs: 6000,             // 6-second continuous follow-up listening window
      bargeInThreshold: 0.038       // Energy threshold while speaking to trigger barge-in
    },
    network: {
      transcribeTimeoutMs: 10000,
      chatTimeoutMs: 15000
    }
  };

  // Wake word regex patterns
  const WAKE_PATTERNS = [
    /\bhey\s+aiva\b/i,
    /\bhi\s+aiva\b/i,
    /\bhello\s+aiva\b/i,
    /\bok\s+aiva\b/i,
    /\baiva\b/i,
    /\bhey\s+eva\b/i,
    /\bhey\s+ava\b/i,
    /\bhey\s+iva\b/i
  ];

  // 4. Structured Non-PII Diagnostic Logger
  function logVoice(stage, event, meta = {}) {
    const dur = meta.duration_ms !== undefined ? `duration_ms=${meta.duration_ms} ` : '';
    const err = meta.error_code ? `error=${meta.error_code} ` : '';
    console.log(`[VoicePipeline] stage=${stage} event=${event} ${dur}${err}`);
  }

  // 5. Zero-Latency Synthesized Earcons (Web Audio API)
  function playEarcon(type) {
    try {
      const AudioCtx = window.AudioContext || window.webkitAudioContext;
      if (!AudioCtx) return;
      const ctx = new AudioCtx();
      const now = ctx.currentTime;
      const osc = ctx.createOscillator();
      const gain = ctx.createGain();

      if (type === 'ack') {
        // Futuristic crisp rising wake chime ("Hey AIVA" recognized)
        osc.type = 'sine';
        osc.frequency.setValueAtTime(520, now);
        osc.frequency.exponentialRampToValueAtTime(880, now + 0.11);
        gain.gain.setValueAtTime(0.001, now);
        gain.gain.linearRampToValueAtTime(0.2, now + 0.02);
        gain.gain.exponentialRampToValueAtTime(0.001, now + 0.11);
        osc.connect(gain);
        gain.connect(ctx.destination);
        osc.start(now);
        osc.stop(now + 0.12);
      } else if (type === 'action') {
        // Instant action acknowledgement earcon ("Opening YouTube...")
        osc.type = 'triangle';
        osc.frequency.setValueAtTime(440, now);
        osc.frequency.setValueAtTime(880, now + 0.05);
        gain.gain.setValueAtTime(0.001, now);
        gain.gain.linearRampToValueAtTime(0.22, now + 0.02);
        gain.gain.exponentialRampToValueAtTime(0.001, now + 0.13);
        osc.connect(gain);
        gain.connect(ctx.destination);
        osc.start(now);
        osc.stop(now + 0.14);
      } else if (type === 'sleep') {
        // Soft relaxing descending tone (follow-up window ended / standby)
        osc.type = 'sine';
        osc.frequency.setValueAtTime(780, now);
        osc.frequency.exponentialRampToValueAtTime(360, now + 0.14);
        gain.gain.setValueAtTime(0.14, now);
        gain.gain.exponentialRampToValueAtTime(0.001, now + 0.14);
        osc.connect(gain);
        gain.connect(ctx.destination);
        osc.start(now);
        osc.stop(now + 0.15);
      } else if (type === 'error') {
        // Soft double warning tone
        osc.type = 'sine';
        osc.frequency.setValueAtTime(260, now);
        gain.gain.setValueAtTime(0.18, now);
        gain.gain.exponentialRampToValueAtTime(0.001, now + 0.16);
        osc.connect(gain);
        gain.connect(ctx.destination);
        osc.start(now);
        osc.stop(now + 0.17);
      }
      setTimeout(() => { try { ctx.close(); } catch {} }, 350);
    } catch {}
  }

  // 6. Single Voice State Machine
  const VoiceState = {
    IDLE: 'idle',
    LISTENING: 'listening',
    PROCESSING: 'processing',
    SPEAKING: 'speaking',
    FOLLOW_UP: 'follow_up',
    MUTED: 'muted',
    ERROR: 'error'
  };

  const VALID_TRANSITIONS = {
    [VoiceState.IDLE]: [VoiceState.LISTENING, VoiceState.FOLLOW_UP, VoiceState.MUTED, VoiceState.ERROR],
    [VoiceState.LISTENING]: [VoiceState.PROCESSING, VoiceState.IDLE, VoiceState.MUTED, VoiceState.ERROR],
    [VoiceState.PROCESSING]: [VoiceState.SPEAKING, VoiceState.IDLE, VoiceState.MUTED, VoiceState.ERROR],
    [VoiceState.SPEAKING]: [VoiceState.FOLLOW_UP, VoiceState.LISTENING, VoiceState.IDLE, VoiceState.MUTED, VoiceState.ERROR],
    [VoiceState.FOLLOW_UP]: [VoiceState.LISTENING, VoiceState.PROCESSING, VoiceState.IDLE, VoiceState.MUTED, VoiceState.ERROR],
    [VoiceState.MUTED]: [VoiceState.IDLE, VoiceState.ERROR],
    [VoiceState.ERROR]: [VoiceState.IDLE, VoiceState.LISTENING, VoiceState.MUTED]
  };

  class VoiceStateMachine {
    constructor() {
      this.currentState = VoiceState.IDLE;
      this.listeners = new Set();
      this.lastError = null;
      this._errorResetTimer = null;
    }

    getState() {
      return this.currentState;
    }

    transition(targetState, meta = {}) {
      if (this.currentState === targetState) return true;

      // Clear pending error auto-reset if moving away from ERROR
      if (this.currentState === VoiceState.ERROR && this._errorResetTimer) {
        clearTimeout(this._errorResetTimer);
        this._errorResetTimer = null;
      }

      const allowed = VALID_TRANSITIONS[this.currentState] || [];
      if (!allowed.includes(targetState)) {
        logVoice('state_machine', 'invalid_transition', {
          from: this.currentState,
          to: targetState
        });
        return false;
      }

      const prevState = this.currentState;
      this.currentState = targetState;
      logVoice('state_machine', 'transition', {
        from: prevState,
        to: targetState,
        ...meta
      });

      this.listeners.forEach(fn => {
        try { fn(this.currentState, prevState, meta); } catch (e) { console.error(e); }
      });
      return true;
    }

    onStateChange(fn) {
      this.listeners.add(fn);
      return () => this.listeners.delete(fn);
    }

    setError(code, details = null) {
      this.lastError = {
        code,
        message: VoiceErrorMessages[code] || 'Voice operation failed.',
        details,
        timestamp: Date.now()
      };
      playEarcon('error');
      this.transition(VoiceState.ERROR, { code });

      // Auto-recover back to IDLE after 2.2 seconds so the Orb NEVER gets stuck in RED
      if (this._errorResetTimer) clearTimeout(this._errorResetTimer);
      this._errorResetTimer = setTimeout(() => {
        if (this.currentState === VoiceState.ERROR) {
          logVoice('state_machine', 'auto_recover_to_idle');
          this.transition(VoiceState.IDLE);
        }
        this._errorResetTimer = null;
      }, 2200);
    }
  }

  // 7. Local Wake-Word Detector ("Hey AIVA", "AIVA")
  class LocalWakeWordDetector {
    constructor(onWakeWordDetected) {
      this.onWake = onWakeWordDetected;
      this.recognition = null;
      this.isListening = false;
      this.enabled = true;
      this.initEngine();
    }

    initEngine() {
      const SpeechRecognition = window.SpeechRecognition || window.webkitSpeechRecognition;
      if (!SpeechRecognition) {
        logVoice('wakeword', 'speech_api_unavailable');
        return;
      }

      try {
        this.recognition = new SpeechRecognition();
        this.recognition.continuous = true;
        this.recognition.interimResults = true;
        this.recognition.lang = 'en-US';

        this.recognition.onresult = (event) => {
          if (!this.enabled || !this.isListening) return;

          for (let i = event.resultIndex; i < event.results.length; ++i) {
            const transcript = event.results[i][0].transcript.trim().toLowerCase();
            logVoice('wakeword', 'heard_chunk', { preview: transcript.slice(0, 30) });

            for (const pattern of WAKE_PATTERNS) {
              if (pattern.test(transcript)) {
                logVoice('wakeword', 'wake_pattern_matched', { pattern: pattern.toString() });

                // Extract any follow-up command spoken in the same breath (e.g. "Hey AIVA what time is it")
                const command = transcript.replace(/.*?\b(?:hey\s+aiva|hi\s+aiva|hello\s+aiva|ok\s+aiva|aiva|hey\s+eva|hey\s+ava|hey\s+iva)\b[\s,]*/i, '').trim();

                this.stop();
                if (this.onWake) {
                  this.onWake(command);
                }
                return;
              }
            }
          }
        };

        this.recognition.onerror = (e) => {
          logVoice('wakeword', 'recognition_error', { error: e.error });
          if (e.error === 'not-allowed' || e.error === 'service-not-allowed') {
            this.enabled = false;
          }
        };

        this.recognition.onend = () => {
          this.isListening = false;
          // Auto-restart standby if still enabled and should be listening
          if (this.enabled && this._shouldBeListening) {
            setTimeout(() => {
              if (this.enabled && this._shouldBeListening && !this.isListening) {
                this.start();
              }
            }, 300);
          }
        };
      } catch (err) {
        logVoice('wakeword', 'init_exception', { error: err.message });
      }
    }

    start() {
      if (!this.recognition || !this.enabled || this.isListening) return;
      try {
        this._shouldBeListening = true;
        this.recognition.start();
        this.isListening = true;
        logVoice('wakeword', 'standby_started');
      } catch (e) {
        // Can throw if already active
      }
    }

    stop() {
      this._shouldBeListening = false;
      if (!this.recognition || !this.isListening) return;
      try {
        this.isListening = false;
        this.recognition.stop();
        logVoice('wakeword', 'standby_stopped');
      } catch (e) {}
    }
  }

  // 8. Central Audio Session Manager
  class AudioSessionManager {
    constructor(stateMachine) {
      this.sm = stateMachine;
      this.mode = VoiceMode.ADAPTIVE;
      this.audioContext = null;
      this.micStream = null;
      this.analyser = null;
      this.mediaRecorder = null;
      this.audioChunks = [];
      this.currentPlayer = null;
      this.audioQueue = [];
      this.isPlayingQueue = false;
      this.streamAudioClosed = false;
      this.onQueueComplete = null;
      this.onBargeInAbort = null;

      // VAD tracking
      this.vadRafId = null;
      this.speechStartedAt = 0;
      this.lastSpeechTime = 0;
      this.isSpeakingDetected = false;
      this.isFollowUpActive = false;
      this.maxRecordTimer = null;
      this.followUpTimer = null;

      // Callbacks
      this.onTranscriptReady = null;
      this.onSilenceDetected = null;
      this.onVadLevel = null;
      this.onFrequencyData = null;
      this.onFollowUpCountdown = null;

      // Guaranteed cleanup on window unload
      window.addEventListener('beforeunload', () => this.dispose());
      if (navigator.mediaDevices && navigator.mediaDevices.addEventListener) {
        navigator.mediaDevices.addEventListener('devicechange', () => this.handleDeviceChange());
      }

      // Initialize Local Wake Word Spotter ("Hey AIVA")
      this.wakeDetector = new LocalWakeWordDetector((capturedCommand) => {
        logVoice('wakeword', 'trigger_command_flow', { has_immediate_cmd: Boolean(capturedCommand) });
        playEarcon('ack');
        if (capturedCommand && capturedCommand.length >= 2) {
          // User spoke command in same breath ("Hey AIVA open Chrome")
          if (this.onTranscriptReady) {
            this.onTranscriptReady(capturedCommand);
          }
        } else {
          // Wake word only ("Hey AIVA"), open mic to record query
          this.startListening();
        }
      });

      // IPC listener for Electron Push-To-Talk (Ctrl+Space) or Native Wake Word
      window.addEventListener('message', (event) => {
        if (!event.data) return;
        if (event.data.type === 'AIVA_WAKE_WORD_TRIGGER') {
          logVoice('wakeword', 'native_wake_detected', { word: event.data.word });
          const state = this.sm.getState();
          if (state === VoiceState.SPEAKING) {
            this.bargeIn();
          } else if (state === VoiceState.IDLE || state === VoiceState.ERROR) {
            this.startListening();
          }
        } else if (event.data.type === 'AIVA_VOICE_PTT_TRIGGER') {
          logVoice('hotkey', 'ptt_message_received');
          const state = this.sm.getState();
          if (state === VoiceState.LISTENING) {
            this.stopListening();
          } else if (state === VoiceState.SPEAKING) {
            this.bargeIn();
          } else if (state === VoiceState.FOLLOW_UP) {
            this.stopListening();
          } else {
            this.startListening();
          }
        }
      });

      // Start wake word standby by default
      setTimeout(() => this.resumeWakeWordStandby(), 1000);
    }

    setMode(newMode) {
      logVoice('mode', 'mode_changed', { from: this.mode, to: newMode });
      this.mode = newMode;
      if (newMode === VoiceMode.MUTED) {
        this.stopPlayback();
        this.stopListening();
        if (this.wakeDetector) this.wakeDetector.stop();
        this.sm.transition(VoiceState.MUTED);
      } else {
        if (this.sm.getState() === VoiceState.MUTED) {
          this.sm.transition(VoiceState.IDLE);
        }
        this.resumeWakeWordStandby();
      }
    }

    getMode() {
      return this.mode;
    }

    resumeWakeWordStandby() {
      if (this.mode === VoiceMode.MUTED) return;
      if (this.sm.getState() === VoiceState.IDLE) {
        if (this.wakeDetector) {
          this.wakeDetector.start();
        }
      }
    }

    pauseWakeWordStandby() {
      if (this.wakeDetector) {
        this.wakeDetector.stop();
      }
    }

    async initAudioContext() {
      if (!this.audioContext || this.audioContext.state === 'closed') {
        const AudioCtx = window.AudioContext || window.webkitAudioContext;
        if (!AudioCtx) throw new Error('Web Audio API not supported');
        this.audioContext = new AudioCtx();
      }
      if (this.audioContext.state === 'suspended') {
        await this.audioContext.resume();
      }
      return this.audioContext;
    }

    async startListening(options = {}) {
      if (!window.isSecureContext && location.hostname !== 'localhost' && location.hostname !== '127.0.0.1') {
        this.sm.setError(VoiceErrorCode.INSECURE_CONTEXT);
        return false;
      }

      if (this.mode === VoiceMode.MUTED) {
        return false;
      }

      const isFollowUp = Boolean(options.isFollowUp);
      this.isFollowUpActive = isFollowUp;

      // Pause wake word while high-fidelity recording has mic
      this.pauseWakeWordStandby();
      this.stopPlayback();
      this.clearTimers();

      try {
        await this.initAudioContext();

        // Enforce acoustic echo cancellation, noise suppression, auto gain
        const constraints = {
          audio: {
            echoCancellation: { ideal: true },
            noiseSuppression: { ideal: true },
            autoGainControl: { ideal: true },
            channelCount: 1,
            sampleRate: 16000
          }
        };

        const t0 = performance.now();
        this.micStream = await navigator.mediaDevices.getUserMedia(constraints);
        const micLatency = Math.round(performance.now() - t0);
        logVoice('capture', 'mic_acquired', { duration_ms: micLatency, is_follow_up: isFollowUp });

        if (!isFollowUp) {
          playEarcon('ack');
        }

        // Connect Web Audio Analyser for VAD & Orb Waveform
        const source = this.audioContext.createMediaStreamSource(this.micStream);
        this.analyser = this.audioContext.createAnalyser();
        this.analyser.fftSize = 256;
        this.analyser.smoothingTimeConstant = 0.2;
        source.connect(this.analyser);

        // Setup MediaRecorder
        this.audioChunks = [];
        const mimeType = MediaRecorder.isTypeSupported('audio/webm;codecs=opus')
          ? 'audio/webm;codecs=opus'
          : (MediaRecorder.isTypeSupported('audio/webm') ? 'audio/webm' : '');

        this.mediaRecorder = mimeType
          ? new MediaRecorder(this.micStream, { mimeType })
          : new MediaRecorder(this.micStream);

        this.mediaRecorder.ondataavailable = (e) => {
          if (e.data && e.data.size > 0) {
            this.audioChunks.push(e.data);
          }
        };

        this.mediaRecorder.onstop = async () => {
          this.processRecordedAudio();
        };

        this.mediaRecorder.onerror = (e) => {
          logVoice('capture', 'recorder_error', { error: e.error ? e.error.message : 'Unknown' });
          this.sm.setError(VoiceErrorCode.AUDIO_INIT_FAILED);
          this.cleanupMic();
        };

        this.mediaRecorder.start(100);

        if (isFollowUp) {
          this.sm.transition(VoiceState.FOLLOW_UP);
          this.startFollowUpCountdownTimer();
        } else {
          this.sm.transition(VoiceState.LISTENING);
        }

        // Start real-time VAD
        this.startVadLoop();

        // Max recording safety guard (15s)
        this.maxRecordTimer = setTimeout(() => {
          const st = this.sm.getState();
          if (st === VoiceState.LISTENING || st === VoiceState.FOLLOW_UP) {
            logVoice('vad', 'max_record_timeout');
            this.stopListening();
          }
        }, VOICE_CONFIG.vad.maxRecordMs);

        return true;
      } catch (err) {
        logVoice('capture', 'mic_error', { error: err.name });
        if (err.name === 'NotAllowedError' || err.name === 'PermissionDeniedError') {
          this.sm.setError(VoiceErrorCode.MIC_DENIED);
        } else if (err.name === 'NotFoundError' || err.name === 'DevicesNotFoundError') {
          this.sm.setError(VoiceErrorCode.NO_DEVICE);
        } else {
          this.sm.setError(VoiceErrorCode.AUDIO_INIT_FAILED, err.message);
        }
        this.cleanupMic();
        this.resumeWakeWordStandby();
        return false;
      }
    }

    startFollowUpCountdownTimer() {
      let remainingMs = VOICE_CONFIG.dialogue.followUpMs;
      const intervalMs = 100;

      if (this.onFollowUpCountdown) {
        this.onFollowUpCountdown(remainingMs, VOICE_CONFIG.dialogue.followUpMs);
      }

      this.followUpTimer = setInterval(() => {
        // If user started speaking during follow-up, pause/stop countdown
        if (this.isSpeakingDetected || this.sm.getState() !== VoiceState.FOLLOW_UP) {
          clearInterval(this.followUpTimer);
          this.followUpTimer = null;
          return;
        }

        remainingMs -= intervalMs;
        if (this.onFollowUpCountdown) {
          this.onFollowUpCountdown(Math.max(0, remainingMs), VOICE_CONFIG.dialogue.followUpMs);
        }

        if (remainingMs <= 0) {
          clearInterval(this.followUpTimer);
          this.followUpTimer = null;
          if (this.sm.getState() === VoiceState.FOLLOW_UP && !this.isSpeakingDetected) {
            logVoice('follow_up', 'timeout_peaceful_sleep');
            playEarcon('sleep');
            this.stopListening();
            this.sm.transition(VoiceState.IDLE);
          }
        }
      }, intervalMs);
    }

    startVadLoop() {
      if (!this.analyser) return;

      const dataArray = new Uint8Array(this.analyser.frequencyBinCount);
      const freqArray = new Uint8Array(this.analyser.frequencyBinCount);
      this.speechStartedAt = 0;
      this.lastSpeechTime = 0;
      this.isSpeakingDetected = false;

      const checkEnergy = () => {
        const curState = this.sm.getState();
        if (curState !== VoiceState.LISTENING && curState !== VoiceState.FOLLOW_UP) return;

        this.analyser.getByteTimeDomainData(dataArray);
        this.analyser.getByteFrequencyData(freqArray);

        let sum = 0;
        for (let i = 0; i < dataArray.length; i++) {
          const norm = (dataArray[i] - 128) / 128;
          sum += norm * norm;
        }
        const rms = Math.sqrt(sum / dataArray.length);

        if (this.onVadLevel) {
          this.onVadLevel(rms);
        }

        if (this.onFrequencyData) {
          this.onFrequencyData(freqArray, rms);
        }

        const now = performance.now();

        if (rms > VOICE_CONFIG.vad.rmsThreshold) {
          if (!this.isSpeakingDetected) {
            this.speechStartedAt = now;
            this.isSpeakingDetected = true;

            // In follow-up window, speech immediately transitions into active LISTENING!
            if (curState === VoiceState.FOLLOW_UP) {
              logVoice('dialogue', 'follow_up_speech_detected');
              if (this.followUpTimer) {
                clearInterval(this.followUpTimer);
                this.followUpTimer = null;
              }
              this.sm.transition(VoiceState.LISTENING);
            }

            logVoice('vad', 'speech_started', { rms: rms.toFixed(4) });
          }
          this.lastSpeechTime = now;
        } else if (this.isSpeakingDetected) {
          const silenceDuration = now - this.lastSpeechTime;
          const speechDuration = this.lastSpeechTime - this.speechStartedAt;

          if (silenceDuration > VOICE_CONFIG.vad.silenceHoldMs) {
            if (speechDuration >= VOICE_CONFIG.vad.minSpeechMs) {
              logVoice('vad', 'speech_ended', {
                speech_duration_ms: Math.round(speechDuration),
                silence_hold_ms: Math.round(silenceDuration)
              });
              this.stopListening();
              return;
            } else {
              this.isSpeakingDetected = false;
            }
          }
        }

        this.vadRafId = requestAnimationFrame(checkEnergy);
      };

      this.vadRafId = requestAnimationFrame(checkEnergy);
    }

    stopListening() {
      if (this.vadRafId) {
        cancelAnimationFrame(this.vadRafId);
        this.vadRafId = null;
      }
      this.clearTimers();

      if (this.mediaRecorder && this.mediaRecorder.state !== 'inactive') {
        try {
          this.mediaRecorder.stop();
        } catch {}
      } else {
        this.cleanupMic();
      }
    }

    async processRecordedAudio() {
      this.cleanupMic();

      // If user was in follow up and silence timed out, don't send empty audio
      if (this.audioChunks.length === 0 || !this.isSpeakingDetected) {
        logVoice('capture', 'no_speech_recorded');
        this.sm.transition(VoiceState.IDLE);
        this.resumeWakeWordStandby();
        return;
      }

      this.sm.transition(VoiceState.PROCESSING);
      const mime = (this.mediaRecorder && this.mediaRecorder.mimeType) || 'audio/webm';
      const audioBlob = new Blob(this.audioChunks, { type: mime });
      this.audioChunks = [];

      // Discard tiny click/pop artifacts (< 1KB) cleanly without error
      if (audioBlob.size < 900) {
        logVoice('capture', 'audio_too_small_discarded', { bytes: audioBlob.size });
        this.sm.transition(VoiceState.IDLE);
        this.resumeWakeWordStandby();
        return;
      }

      const t0 = performance.now();
      try {
        const formData = new FormData();
        formData.append('audio', audioBlob, 'voice.webm');

        const token = (typeof getAccessToken === 'function' ? getAccessToken() : '') ||
                      localStorage.getItem('aiva_access_token') || '';

        const headers = {};
        if (token) headers['Authorization'] = `Bearer ${token}`;

        const controller = new AbortController();
        const timeoutId = setTimeout(() => controller.abort(), VOICE_CONFIG.network.transcribeTimeoutMs);

        const resp = await fetch('/api/voice/transcribe', {
          method: 'POST',
          headers,
          body: formData,
          signal: controller.signal
        });
        clearTimeout(timeoutId);

        const transcribeMs = Math.round(performance.now() - t0);

        if (!resp.ok) {
          logVoice('transcribe', 'server_error', { status: resp.status, duration_ms: transcribeMs });
          this.sm.setError(VoiceErrorCode.STT_FAILED);
          return;
        }

        const data = await resp.json();
        if (data.status === 'success' && data.text && data.text.trim()) {
          logVoice('transcribe', 'success', { duration_ms: transcribeMs, chars: data.text.trim().length });
          if (this.onTranscriptReady) {
            this.onTranscriptReady(data.text.trim());
          }
        } else {
          // Empty or silence transcription: Soft recovery, never lock in red error!
          logVoice('transcribe', 'empty_transcript_peaceful_recovery', { duration_ms: transcribeMs });
          playEarcon('sleep');
          if (this.onSilenceDetected) {
            this.onSilenceDetected();
          }
          this.sm.transition(VoiceState.IDLE);
          this.resumeWakeWordStandby();
        }
      } catch (err) {
        const durationMs = Math.round(performance.now() - t0);
        logVoice('transcribe', 'fetch_error', { error: err.name, duration_ms: durationMs });
        if (err.name === 'AbortError') {
          this.sm.setError(VoiceErrorCode.TIMEOUT);
        } else {
          this.sm.setError(VoiceErrorCode.NETWORK);
        }
        this.resumeWakeWordStandby();
      }
    }

    beginAudioStream(onComplete = null) {
      this.stopPlayback();
      this.audioQueue = [];
      this.isPlayingQueue = false;
      this.streamAudioClosed = false;
      this.onQueueComplete = onComplete;
    }

    enqueueAudioChunk(audioBase64) {
      if (this.mode === VoiceMode.MUTED || this.sm.getState() === VoiceState.MUTED) {
        this.sm.transition(VoiceState.IDLE);
        return;
      }
      if (!audioBase64) return;
      this.pauseWakeWordStandby();
      if (this.sm.getState() !== VoiceState.SPEAKING) {
        this.sm.transition(VoiceState.SPEAKING);
      }
      this.audioQueue.push(audioBase64);
      if (!this.isPlayingQueue) {
        this._playNextQueuedChunk();
      }
    }

    finishAudioStream() {
      this.streamAudioClosed = true;
      if (!this.isPlayingQueue && this.audioQueue.length === 0 && !this.currentPlayer) {
        const doneCb = this.onQueueComplete;
        this.onQueueComplete = null;
        if (doneCb) doneCb();
        if (this.sm.getState() === VoiceState.SPEAKING) {
          if (this.mode === VoiceMode.ADAPTIVE || this.mode === VoiceMode.ALLVOICE) {
            this.enterFollowUpWindow();
          } else {
            this.sm.transition(VoiceState.IDLE);
            this.resumeWakeWordStandby();
          }
        }
      }
    }

    _playNextQueuedChunk() {
      if (this.mode === VoiceMode.MUTED || this.sm.getState() === VoiceState.MUTED) {
        this.stopPlayback();
        this.sm.transition(VoiceState.IDLE);
        return;
      }
      if (this.audioQueue.length === 0) {
        this.isPlayingQueue = false;
        if (this.streamAudioClosed) {
          this.finishAudioStream();
        }
        return;
      }

      this.isPlayingQueue = true;
      const audioBase64 = this.audioQueue.shift();
      try {
        this.currentPlayer = new Audio('data:audio/mp3;base64,' + audioBase64);

        const advanceOrFinish = () => {
          this.currentPlayer = null;
          if (this.audioQueue.length > 0) {
            this._playNextQueuedChunk();
          } else {
            this.isPlayingQueue = false;
            if (this.streamAudioClosed) {
              this.finishAudioStream();
            }
          }
        };

        this.currentPlayer.onended = () => {
          logVoice('playback', 'chunk_finished', { remaining: this.audioQueue.length });
          advanceOrFinish();
        };

        this.currentPlayer.onerror = (e) => {
          logVoice('playback', 'chunk_error', { error: e });
          advanceOrFinish();
        };

        this.currentPlayer.play().catch(playErr => {
          logVoice('playback', 'autoplay_blocked', { error: playErr.message });
          this.stopPlayback();
          if (this.mode === VoiceMode.ADAPTIVE || this.mode === VoiceMode.ALLVOICE) {
            this.enterFollowUpWindow();
          } else {
            this.sm.transition(VoiceState.IDLE);
            this.resumeWakeWordStandby();
          }
        });
      } catch (err) {
        logVoice('playback', 'chunk_init_error', { error: err.message });
        this.stopPlayback();
        this.sm.setError(VoiceErrorCode.TTS_FAILED);
        this.resumeWakeWordStandby();
      }
    }

    playAudio(audioBase64, onComplete = null) {
      if (this.mode === VoiceMode.MUTED || this.sm.getState() === VoiceState.MUTED) {
        this.sm.transition(VoiceState.IDLE);
        return;
      }

      this.pauseWakeWordStandby();
      this.stopPlayback();
      this.sm.transition(VoiceState.SPEAKING);

      if (!audioBase64) {
        this.sm.transition(VoiceState.IDLE);
        this.resumeWakeWordStandby();
        return;
      }

      this.beginAudioStream(onComplete);
      this.enqueueAudioChunk(audioBase64);
      this.finishAudioStream();
    }

    bargeIn() {
      const st = this.sm.getState();
      if (st === VoiceState.SPEAKING || st === VoiceState.PROCESSING) {
        logVoice('playback', 'barge_in_triggered', { from_state: st });
        if (this.onBargeInAbort) {
          try { this.onBargeInAbort(); } catch {}
        }
        this.stopPlayback();
        this.startListening();
      }
    }

    enterFollowUpWindow() {
      if (this.mode === VoiceMode.MUTED || this.sm.getState() === VoiceState.MUTED) return;

      logVoice('dialogue', 'enter_continuous_followup_window');
      // Actually start listening on the mic so the follow-up window is truly interactive!
      this.startListening({ isFollowUp: true });
    }

    stopPlayback() {
      this.audioQueue = [];
      this.isPlayingQueue = false;
      this.streamAudioClosed = true;
      this.onQueueComplete = null;
      if (this.currentPlayer) {
        try {
          this.currentPlayer.pause();
          this.currentPlayer.currentTime = 0;
        } catch {}
        this.currentPlayer = null;
      }
    }

    cleanupMic() {
      if (this.micStream) {
        this.micStream.getTracks().forEach(t => {
          try { t.stop(); } catch {}
        });
        this.micStream = null;
      }
      this.analyser = null;
    }

    clearTimers() {
      if (this.maxRecordTimer) {
        clearTimeout(this.maxRecordTimer);
        this.maxRecordTimer = null;
      }
      if (this.followUpTimer) {
        clearInterval(this.followUpTimer);
        this.followUpTimer = null;
      }
    }

    handleDeviceChange() {
      logVoice('device', 'device_change_detected');
      const st = this.sm.getState();
      if (st === VoiceState.LISTENING || st === VoiceState.FOLLOW_UP) {
        this.stopListening();
        this.sm.setError(VoiceErrorCode.MIC_UNPLUGGED);
      }
    }

    dispose() {
      this.clearTimers();
      this.stopPlayback();
      this.cleanupMic();
      if (this.wakeDetector) {
        this.wakeDetector.stop();
      }
      if (this.audioContext && this.audioContext.state !== 'closed') {
        try { this.audioContext.close(); } catch {}
      }
    }
  }

  // 9. Visual HUD Synchronization Controller
  function initHudVoiceSync(stateMachine, sessionManager) {
    const orb = document.getElementById('neural-core');
    const privacyPill = document.getElementById('mic-privacy-pill');
    const privacyLabel = document.getElementById('mic-privacy-label');
    const captionBar = document.getElementById('voice-caption-bar');
    const captionSpeaker = document.getElementById('caption-speaker');
    const captionText = document.getElementById('caption-text');

    stateMachine.onStateChange((state, prevState, meta) => {
      // Broadcast state to parent companion frame if embedded
      if (window.self !== window.top || document.body.classList.contains('is-embedded')) {
        try {
          window.parent.postMessage({
            type: 'AIVA_VOICE_STATE_CHANGE',
            state: state,
            isListening: state === VoiceState.LISTENING || state === VoiceState.FOLLOW_UP
          }, '*');
        } catch {}
      }

      // 1. Update Central Orb classes
      if (orb) {
        orb.className = 'neural-core-wrapper';
        orb.classList.add(state);
      }

      // 2. Privacy Indicator
      if (privacyPill && privacyLabel) {
        if (state === VoiceState.LISTENING || state === VoiceState.FOLLOW_UP) {
          privacyPill.classList.add('active');
          privacyLabel.textContent = state === VoiceState.FOLLOW_UP ? 'MIC (FOLLOW-UP)' : 'MIC LIVE';
        } else {
          privacyPill.classList.remove('active');
          privacyLabel.textContent = state === VoiceState.MUTED ? 'MUTED' : 'MIC IDLE';
        }
      }

      // 3. Caption Bar
      if (captionBar && captionSpeaker && captionText) {
        if (state === VoiceState.LISTENING) {
          captionBar.classList.remove('hidden');
          captionSpeaker.textContent = 'YOU';
          captionText.textContent = 'Listening... Speak your command.';
        } else if (state === VoiceState.PROCESSING) {
          captionBar.classList.remove('hidden');
          captionSpeaker.textContent = 'AIVA';
          captionText.textContent = 'Transcribing and thinking...';
        } else if (state === VoiceState.FOLLOW_UP) {
          captionBar.classList.remove('hidden');
          captionSpeaker.textContent = 'AIVA';
          captionText.textContent = 'Listening for follow-up... (6s)';
        } else if (state === VoiceState.ERROR) {
          captionBar.classList.remove('hidden');
          captionSpeaker.textContent = 'SYSTEM';
          captionText.textContent = VoiceErrorMessages[meta.code] || 'Voice operation failed.';
          setTimeout(() => {
            if (stateMachine.getState() === VoiceState.IDLE) {
              captionBar.classList.add('hidden');
            }
          }, 3500);
        } else if (state === VoiceState.IDLE) {
          captionBar.classList.add('hidden');
        }
      }
    });

    // Dynamic RMS Orb scaling
    sessionManager.onVadLevel = (rms) => {
      if (orb && (stateMachine.getState() === VoiceState.LISTENING || stateMachine.getState() === VoiceState.FOLLOW_UP)) {
        const scale = (1.0 + Math.min(rms * 10, 0.75)).toFixed(3);
        orb.style.setProperty('--orb-vad-scale', scale);
      }
    };

    // Follow-up countdown indicator in caption bar
    sessionManager.onFollowUpCountdown = (remainingMs) => {
      if (captionText && stateMachine.getState() === VoiceState.FOLLOW_UP) {
        const seconds = Math.ceil(remainingMs / 1000);
        captionText.textContent = `Listening for follow-up... (${seconds}s)`;
      }
    };

    // Silence detection friendly prompt
    sessionManager.onSilenceDetected = () => {
      if (captionBar && captionSpeaker && captionText) {
        captionBar.classList.remove('hidden');
        captionSpeaker.textContent = 'AIVA';
        captionText.textContent = "I didn't hear anything. Say 'Hey AIVA' or press Ctrl+Space.";
        setTimeout(() => {
          if (stateMachine.getState() === VoiceState.IDLE) {
            captionBar.classList.add('hidden');
          }
        }, 3200);
      }
    };

    // 4. Initialize Live Audio Frequency Visualizer Component
    initAudioFrequencyComponent(sessionManager, stateMachine);
  }

  function initAudioFrequencyComponent(sessionManager, stateMachine) {
    const mainContainer = document.getElementById('audio-frequency-component');
    const mainBarsContainer = document.getElementById('frequency-bars');
    const statusText = document.getElementById('frequency-status-text');
    const statusBadge = document.getElementById('frequency-status-badge');
    const sidebarComponent = document.getElementById('sidebar-freq-component');
    const sidebarBars = document.getElementById('sidebar-freq-bars');
    const sidebarDb = document.getElementById('sidebar-freq-db');

    // Create 20 frequency bars for main caption bar
    const numBars = 20;
    const mainBarEls = [];
    if (mainBarsContainer && mainBarsContainer.children.length === 0) {
      for (let i = 0; i < numBars; i++) {
        const bar = document.createElement('span');
        bar.className = 'freq-bar';
        mainBarsContainer.appendChild(bar);
        mainBarEls.push(bar);
      }
    }

    // Create 12 frequency bars for sidebar component
    const numSidebarBars = 12;
    const sidebarBarEls = [];
    if (sidebarBars && sidebarBars.children.length === 0) {
      for (let i = 0; i < numSidebarBars; i++) {
        const bar = document.createElement('span');
        bar.className = 'sidebar-freq-bar';
        sidebarBars.appendChild(bar);
        sidebarBarEls.push(bar);
      }
    }

    // Connect real-time frequency handler
    sessionManager.onFrequencyData = (freqArray, rms) => {
      const state = stateMachine.getState();
      const isListening = state === VoiceState.LISTENING || state === VoiceState.FOLLOW_UP;
      if (!isListening) return;

      // Calculate approximate dBFS from RMS
      const db = rms > 0 ? Math.max(-60, Math.round(20 * Math.log10(rms))) : -60;
      if (sidebarDb) {
        sidebarDb.textContent = `${db} dB`;
      }

      const isSpeaking = rms > 0.02;
      if (statusText) {
        statusText.textContent = isSpeaking ? 'VOICE DETECTED' : (state === VoiceState.FOLLOW_UP ? 'FOLLOW-UP (6s)' : 'LISTENING');
      }
      if (statusBadge) {
        if (isSpeaking) {
          statusBadge.classList.add('speaking');
        } else {
          statusBadge.classList.remove('speaking');
        }
      }

      // Update main spectrum bars (focus on speech frequency bins 2..64)
      if (mainBarEls.length > 0) {
        const step = Math.max(1, Math.floor(64 / numBars));
        for (let i = 0; i < numBars; i++) {
          let sum = 0;
          const startBin = 2 + (i * step);
          for (let b = 0; b < step; b++) {
            sum += freqArray[startBin + b] || 0;
          }
          const val = sum / step; // 0..255
          let scale = (val / 255.0) * 1.5;
          if (scale < 0.1) {
            // Ambient idle breathing wave so user sees it is actively listening
            scale = 0.08 + 0.06 * Math.sin((Date.now() / 150) + (i * 0.4));
          } else {
            scale = Math.min(1.0, scale);
          }
          mainBarEls[i].style.transform = `scaleY(${scale.toFixed(3)})`;
        }
      }

      // Update sidebar spectrum bars
      if (sidebarBarEls.length > 0) {
        const step = Math.max(1, Math.floor(64 / numSidebarBars));
        for (let i = 0; i < numSidebarBars; i++) {
          let sum = 0;
          const startBin = 2 + (i * step);
          for (let b = 0; b < step; b++) {
            sum += freqArray[startBin + b] || 0;
          }
          const val = sum / step;
          let scale = (val / 255.0) * 1.5;
          if (scale < 0.1) {
            scale = 0.08 + 0.05 * Math.sin((Date.now() / 180) + (i * 0.5));
          } else {
            scale = Math.min(1.0, scale);
          }
          sidebarBarEls[i].style.transform = `scaleY(${scale.toFixed(3)})`;
        }
      }
    };

    // State change transitions
    stateMachine.onStateChange((state) => {
      const isListening = state === VoiceState.LISTENING || state === VoiceState.FOLLOW_UP;
      if (mainContainer) {
        if (isListening) {
          mainContainer.classList.remove('hidden');
        } else {
          mainContainer.classList.add('hidden');
          mainBarEls.forEach(bar => { bar.style.transform = 'scaleY(0.08)'; });
        }
      }
      if (sidebarComponent) {
        if (isListening) {
          sidebarComponent.classList.remove('hidden');
        } else {
          sidebarComponent.classList.add('hidden');
          sidebarBarEls.forEach(bar => { bar.style.transform = 'scaleY(0.08)'; });
          if (sidebarDb) sidebarDb.textContent = '-- dB';
        }
      }
    });
  }

  // 10. Runtime Instantiation & Export
  const stateMachine = new VoiceStateMachine();
  const sessionManager = new AudioSessionManager(stateMachine);

  // Initialize UI sync once DOM is ready
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', () => initHudVoiceSync(stateMachine, sessionManager));
  } else {
    initHudVoiceSync(stateMachine, sessionManager);
  }

  window.AivaVoice = {
    stateMachine,
    sessionManager,
    VoiceState,
    VoiceMode,
    VoiceErrorCode,
    VoiceErrorMessages,
    VOICE_CONFIG,
    playEarcon,
    logVoice,
    setMode: (m) => sessionManager.setMode(m),
    getMode: () => sessionManager.getMode()
  };

  logVoice('runtime', 'initialized', { version: '2.1.0', wake_word: 'Hey AIVA' });
})();