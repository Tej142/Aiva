import re
import time
import asyncio
import threading
import numpy as np
try:
    import sounddevice as sd
except (ImportError, OSError):
    sd = None
import speech_recognition as sr
from core.shared.logger import get_logger

logger = get_logger("WakeWordService")

# Robust phonetic wake word variations covering common STT transcriptions
WAKE_PATTERNS = [
    r"\bhey\s+aiva\b",
    r"\bhi\s+aiva\b",
    r"\bhello\s+aiva\b",
    r"\bok\s+aiva\b",
    r"\bhey\s+eva\b",
    r"\bhey\s+ava\b",
    r"\bhey\s+iva\b",
    r"\bhey\s+ever\b",
    r"\bhey\s+i\s+was\b",
    r"\bhey\s+what\b",
    r"\baiva\b",
    r"\bayva\b",
    r"\biva\b",
    r"\beva\b",
    r"\bava\b",
    r"\beye\s+va\b"
]

# Mode constants
MODE_ADAPTIVE = "adaptive"      #  Professional Default: Wake word -> Intelligent Follow-up -> Auto Sleep
MODE_WAKEWORD = "wakeword"      # Strict: Wake word required for every command
MODE_ALLVOICE = "allvoice"      # Continuous: Always listening to all speech

# State constants
STATE_STANDBY    = "standby"     # Passive listening for "Hey AIVA"
STATE_LISTENING  = "listening"   # Actively listening for user command
STATE_PROCESSING = "processing"  # Query processing & intent routing
STATE_SPEAKING   = "speaking"    # TTS speaking aloud
STATE_FOLLOWUP   = "follow_up"   # Adaptive continued conversation window (no wake word needed)


def play_chime(frequency_start: float = 600, frequency_end: float = 900, duration: float = 0.15, volume: float = 0.25):
    """Generates and plays a clean, synthesized futuristic chime tone."""
    try:
        sample_rate = 44100
        t = np.linspace(0, duration, int(sample_rate * duration), False)
        freq = np.linspace(frequency_start, frequency_end, len(t))
        tone = np.sin(2 * np.pi * freq * t)
        window = np.sin(np.pi * t / duration)  # Hann-like envelope for soft onset/offset
        audio = (tone * window * volume).astype(np.float32)
        sd.play(audio, samplerate=sample_rate)
        sd.wait()
    except Exception as e:
        logger.debug(f"Chime error: {e}")


def play_wake_chime():
    """Futuristic crisp rising chirp when wake word is detected."""
    play_chime(550, 950, 0.12, volume=0.28)


def play_followup_chime():
    """Subtle, pleasant micro-chime indicating follow-up listening mode is active."""
    play_chime(680, 840, 0.08, volume=0.16)


def play_sleep_chime():
    """Gentle, relaxing descending tone when returning to standby or dismissed."""
    play_chime(850, 420, 0.15, volume=0.22)


class WakeWordService:
    """
    Adaptive Conversational Voice I/O Runtime for AIVA.

    Features:
    - Mode 'adaptive' (Default):
      1. Sits in STANDBY listening for 'Hey AIVA'.
      2. Activates with wake chime and listens to user query.
      3. Dispatches to kernel; evaluates intent & response.
      4. If conversation continues or task completes, dynamically enters FOLLOW_UP mode.
      5. During FOLLOW_UP, listens for the next command WITHOUT requiring 'Hey AIVA'.
      6. On user dismissal ('thanks', 'that's all', 'bye') or silence timeout, plays
         gentle sleep chime and returns to STANDBY.
    - Full backwards compatibility with manual toggles (wakeword, allvoice).
    - Thread-safe dialogue state machine & EventBus broadcasting.
    """

    def __init__(self, kernel=None):
        self.kernel = kernel
        self.running = False
        self.mode = MODE_ADAPTIVE
        self.state = STATE_STANDBY
        self.followup_timeout = 7.0
        self.is_speaking = False

        self.recognizer = sr.Recognizer()
        self.recognizer.energy_threshold = 240
        self.recognizer.dynamic_energy_threshold = False
        self.recognizer.pause_threshold = 0.9
        self.recognizer.non_speaking_duration = 0.4
        self.recognizer.phrase_threshold = 0.2

        self._thread = None
        self._loop = None
        self._last_followup_time = 0.0
        self._lock = threading.Lock()

    @property
    def continuous_mode(self) -> bool:
        """Compatibility property for legacy callers."""
        return self.mode == MODE_ALLVOICE

    @continuous_mode.setter
    def continuous_mode(self, value: bool):
        self.mode = MODE_ALLVOICE if value else MODE_WAKEWORD

    def set_state(self, new_state: str):
        """Thread-safe state update and event broadcasting."""
        with self._lock:
            self.state = new_state
        logger.info(f"[Voice Runtime] State: [{new_state.upper()}] (Mode: {self.mode})")
        self._emit_state_event(new_state)

    def _emit_state_event(self, state: str):
        """Broadcasts voice state change to Kernel EventBus (which relays to UI HUD)."""
        if self.kernel and hasattr(self.kernel, "event_bus") and self._loop:
            async def emit():
                try:
                    await self.kernel.event_bus.emit({
                        "type": "voice.state_changed",
                        "data": {
                            "state": state,
                            "mode": self.mode,
                            "running": self.running
                        }
                    })
                except Exception:
                    pass
            try:
                asyncio.run_coroutine_threadsafe(emit(), self._loop)
            except Exception:
                pass

    def start_background(self, loop=None, mode: str = MODE_ADAPTIVE, continuous_mode: bool = False):
        """Starts the background adaptive voice listener loop."""
        if continuous_mode:
            self.mode = MODE_ALLVOICE
        else:
            self.mode = mode or MODE_ADAPTIVE

        if self.running:
            self.set_state(STATE_STANDBY)
            return

        self.running = True
        self.set_state(STATE_STANDBY)
        self._loop = loop or asyncio.get_event_loop()
        self._thread = threading.Thread(target=self._listen_loop, daemon=True, name="AIVA-AdaptiveVoice-Thread")
        self._thread.start()

        if self.mode == MODE_ADAPTIVE:
            logger.info("Adaptive Conversational Voice Runtime active (Dynamic turn-taking & follow-up enabled)")
        elif self.mode == MODE_ALLVOICE:
            logger.info("Continuous All-Voice Mode active (Always listening)")
        else:
            logger.info("Strict Wake Word detection active ('Hey AIVA')")

    def stop(self):
        """Stops the voice listener cleanly."""
        self.running = False
        self.set_state(STATE_STANDBY)
        logger.info("Voice runtime stopped")

    def trigger_followup(self, timeout: float = 7.0):
        """
        External hook (e.g. called by OutcomeReporter after task execution)
        to immediately put the voice runtime into follow-up listening mode.
        """
        if not self.running or self.mode != MODE_ADAPTIVE:
            return
        logger.info(f"Triggering follow-up listening window ({timeout}s)")
        self.followup_timeout = timeout
        self._last_followup_time = time.time()
        self.set_state(STATE_FOLLOWUP)
        play_followup_chime()

    def _extract_wake_and_command(self, text: str):
        """Detects wake patterns and extracts the follow-up command cleanly."""
        if not text:
            return False, ""

        clean = text.lower().strip()

        # In continuous all-voice mode, everything spoken is an active command
        if self.mode == MODE_ALLVOICE:
            for pattern in WAKE_PATTERNS:
                match = re.search(pattern, clean)
                if match:
                    end_pos = match.end()
                    cmd = clean[end_pos:].strip(" ,.!?")
                    if match.group(0).strip() == "hey what" and not cmd.startswith("what"):
                        cmd = f"what {cmd}".strip()
                    return True, cmd or clean
            return True, clean

        # In follow-up state, accept any command directly without requiring wake word
        if self.state == STATE_FOLLOWUP:
            # If user said wake word anyway, strip it nicely
            for pattern in WAKE_PATTERNS:
                match = re.search(pattern, clean)
                if match:
                    end_pos = match.end()
                    cmd = clean[end_pos:].strip(" ,.!?")
                    return True, cmd or clean
            return True, clean

        # Standby mode: wake word required
        for pattern in WAKE_PATTERNS:
            match = re.search(pattern, clean)
            if match:
                end_pos = match.end()
                command = clean[end_pos:].strip(" ,.!?")
                if match.group(0).strip() == "hey what" and not command.startswith("what"):
                    command = f"what {command}".strip()
                return True, command

        return False, ""

    def _listen_loop(self):
        """Main adaptive microphone monitoring and dialogue loop."""
        try:
            with sr.Microphone() as source:
                logger.info("Calibrating microphone for ambient room noise...")
                self.recognizer.adjust_for_ambient_noise(source, duration=0.8)
                if self.recognizer.energy_threshold > 350:
                    self.recognizer.energy_threshold = 280

                print("\n [AIVA Voice Runtime]: Ready. Say 'Hey AIVA' to start talking.\n")

                while self.running:
                    # While TTS or action is speaking, pause listening to prevent audio loop
                    if self.is_speaking:
                        time.sleep(0.25)
                        continue

                    current_state = self.state

                    # Differentiated timeouts: shorter in follow-up (conversational pace),
                    # slightly longer in standby (passive waiting is fine)
                    if current_state == STATE_FOLLOWUP:
                        listen_timeout = 3.5   # snappier turn-taking in dialogue
                    else:
                        listen_timeout = 5.0   # relaxed standby listening

                    try:
                        audio = self.recognizer.listen(
                            source,
                            timeout=listen_timeout,
                            phrase_time_limit=12.0
                        )
                    except sr.WaitTimeoutError:
                        # Check if follow-up window expired
                        if self.state == STATE_FOLLOWUP:
                            elapsed = time.time() - self._last_followup_time
                            if elapsed >= self.followup_timeout:
                                logger.info("Follow-up silence timeout. Returning to Standby.")
                                play_sleep_chime()
                                self.set_state(STATE_STANDBY)
                        continue
                    except Exception as e:
                        time.sleep(0.3)
                        continue

                    if self.is_speaking:
                        continue

                    try:
                        transcribed = self.recognizer.recognize_google(audio, language="en-IN")
                        if not transcribed:
                            continue

                        logger.info(f"Heard audio: '{transcribed}' (State: {self.state})")

                        has_wake, command = self._extract_wake_and_command(transcribed)

                        if has_wake:
                            was_followup = (self.state == STATE_FOLLOWUP)

                            if not was_followup and self.mode != MODE_ALLVOICE:
                                play_wake_chime()

                            # If user only said 'Hey AIVA' in standby, prompt and listen for command
                            if not command and not was_followup and self.mode != MODE_ALLVOICE:
                                self.set_state(STATE_LISTENING)
                                print("\n[AIVA]: Listening for your command...")
                                try:
                                    cmd_audio = self.recognizer.listen(source, timeout=6.0, phrase_time_limit=15.0)
                                    command = self.recognizer.recognize_google(cmd_audio, language="en-IN")
                                    print(f"[You Spoke]: {command}")
                                except Exception as err:
                                    logger.debug(f"Follow-up listen timeout/error: {err}")
                                    self.set_state(STATE_STANDBY)
                                    continue

                            if command and self.kernel and self._loop:
                                self.set_state(STATE_PROCESSING)
                                self.is_speaking = True
                                self._dispatch_to_kernel(command)
                                time.sleep(1.2)

                    except sr.UnknownValueError:
                        # Check if follow-up window expired
                        if self.state == STATE_FOLLOWUP:
                            elapsed = time.time() - self._last_followup_time
                            if elapsed >= self.followup_timeout:
                                logger.info("Follow-up expired. Going to standby.")
                                play_sleep_chime()
                                self.set_state(STATE_STANDBY)
                    except sr.RequestError as e:
                        logger.warning(f"Google STT service error: {e}")
                        time.sleep(1.0)
                    except Exception as e:
                        logger.error(f"Error in voice loop: {e}")
                        time.sleep(0.4)

        except Exception as e:
            logger.error(f"Fatal error opening microphone: {e}")

    def _dispatch_to_kernel(self, user_command: str):
        """Sends transcribed voice command to AIVA kernel and evaluates follow-up."""
        try:
            async def handle():
                try:
                    print(f"\n[Voice Command]: {user_command}")
                    self.set_state(STATE_PROCESSING)

                    # Process query through InputHandler
                    result = await self.kernel.input_handler.process_query(user_command, speak=True)
                    if not result:
                        result = {}

                    should_end = result.get("should_end_session", False)
                    expect_followup = result.get("expect_followup", True)
                    followup_to = result.get("followup_timeout", 7.0)

                    # process_query awaits TTS internally (speak=True), so when it
                    # returns the audio is already done. Small tail buffer for reverb.
                    self.set_state(STATE_SPEAKING)
                    await asyncio.sleep(0.8)
                    self.is_speaking = False


                    # Dynamic Turn-Taking Decision
                    if self.mode == MODE_ADAPTIVE:
                        if should_end:
                            # User dismissed or session closed
                            play_sleep_chime()
                            self.set_state(STATE_STANDBY)
                        elif expect_followup:
                            # Keep listening dynamically without wake word
                            self.followup_timeout = followup_to
                            self._last_followup_time = time.time()
                            self.set_state(STATE_FOLLOWUP)
                            play_followup_chime()
                            print(f"\n [AIVA]: Listening for follow-up ({followup_to:.0f}s)... Speak freely.\n")
                        else:
                            self.set_state(STATE_STANDBY)
                    elif self.mode == MODE_ALLVOICE:
                        self.set_state(STATE_STANDBY)
                    else:
                        # Strict wake-word only
                        self.set_state(STATE_STANDBY)

                except Exception as e:
                    logger.error(f"Error executing voice command: {e}")
                    self.is_speaking = False
                    self.set_state(STATE_STANDBY)

            asyncio.run_coroutine_threadsafe(handle(), self._loop)

        except Exception as e:
            logger.error(f"Error dispatching voice command to kernel: {e}")
            self.is_speaking = False
            self.set_state(STATE_STANDBY)
