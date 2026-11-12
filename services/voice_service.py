import os
import re
import time
import asyncio
import tempfile
import uuid
import numpy as np
from core.shared.config_loader import load_secrets
from core.shared.logger import get_logger

logger = get_logger("VoiceService")

# =====================================================================
# VOICE PIPELINE CONFIGURATION (Free Neural Voices - No API Key Needed)
# =====================================================================
VOICE_PRABHAT = "en-IN-PrabhatNeural"   # 🥇 English & Tenglish (Indian Accent)
VOICE_MOHAN   = "te-IN-MohanNeural"     # 🥇 Native Telugu Script

# Tuned for high-energy, fast, natural conversational delivery
RATE_PRABHAT  = "+10%"                  # Lively conversational pace
RATE_MOHAN    = "+5%"                   # Natural Telugu pace
PITCH_PRABHAT = "-1Hz"                  # Calm, crisp tone
PITCH_MOHAN   = "-1Hz"

# Minimal silence pre-roll to prevent clipping while starting instantly
SILENCE_PRE_ROLL_SEC = 0.05

# Max retries for network calls
MAX_RETRIES = 3
RETRY_DELAY = 0.5

# Casual-speech text normalization (applied before TTS so the delivery
# doesn't read like a formal script)
CASUAL_REPLACEMENTS = [
    (r"\bI am\b", "I'm"),
    (r"\bYou are\b", "You're"),
    (r"\bWe are\b", "We're"),
    (r"\bThey are\b", "They're"),
    (r"\bIt is\b", "It's"),
    (r"\bThat is\b", "That's"),
    (r"\bdo not\b", "don't"),
    (r"\bdoes not\b", "doesn't"),
    (r"\bcannot\b", "can't"),
    (r"\bcould not\b", "couldn't"),
    (r"\bwould not\b", "wouldn't"),
    (r"\bshould not\b", "shouldn't"),
    (r"\bI will\b", "I'll"),
    (r"\byou will\b", "you'll"),
    (r"\blet us\b", "let's"),
    (r"\bI have\b", "I've"),
    (r"\byou have\b", "you've"),
]


def _has_telugu_script(text: str) -> bool:
    """Check if the text contains Telugu Unicode script."""
    return bool(re.search(r"[\u0c00-\u0c7f]", text))


def sanitize_for_speech(text: str, max_chars: int = 400) -> str:
    """
    Sanitizes LLM response text into crisp, natural spoken dialogue (Jarvis style).
    - Replaces multi-line code blocks with a clean announcement ("I've displayed the code on your screen.")
    - Inlines simple backticks (`npm install` -> npm install)
    - Expands markdown links [Label](url) -> Label
    - Replaces bare URLs with "the link on screen"
    - Strips markdown formatting symbols (*, _, #, ~, >, table pipes |)
    - Strips ASCII emoticons and Unicode emojis
    - Normalizes casual phrasing ("I am" -> "I'm")
    - Intelligently truncates excessively long multi-paragraph responses at a sentence boundary
      and offers to leave the full details on screen.
    """
    if not text:
        return "Done."

    # 1. Multi-line code blocks
    text = re.sub(r"```[a-zA-Z0-9_\-]*\n[\s\S]*?```", " I've displayed the code on your screen. ", text)
    text = re.sub(r"```[\s\S]*?```", " I've displayed the code on your screen. ", text)

    # 2. Markdown links: [Title](url) -> Title
    text = re.sub(r"\[([^\]]+)\]\([^\)]+\)", r"\1", text)

    # 3. Bare URLs -> "the link on screen"
    text = re.sub(r"https?://\S+", "the link on screen", text)

    # 4. Inline code backticks: `const x = 1` -> const x = 1
    text = re.sub(r"`([^`]+)`", r"\1", text)

    # 5. Table rows & dividers
    text = re.sub(r"\|.*\|", " ", text)
    text = re.sub(r"[-=]{3,}", " ", text)

    # 6. Markdown symbols and list bullets
    text = re.sub(r"[*_#~\[\]>]+", "", text)
    text = re.sub(r"^\s*[-*+]\s+", "", text, flags=re.MULTILINE)

    # 7. ASCII emoticons
    text = re.sub(r"(?<!\w)[:;=8][\-o\*\']?[\)\]\(\[dDpP/\:\}\{@\|\\](?!\w)", "", text)
    text = re.sub(r"(?<!\w)[\)\]\(\[dDpP][\-o\*\']?[:;=8](?!\w)", "", text)
    text = re.sub(r"\bxD\b|\bXD\b|\bhaha\b|\blol\b|\bhehe\b", "", text, flags=re.IGNORECASE)

    # 8. Unicode emojis
    text = re.sub(
        r"[\U0001F600-\U0001F64F"
        r"\U0001F300-\U0001F5FF"
        r"\U0001F680-\U0001F6FF"
        r"\U0001F1E0-\U0001F1FF"
        r"\U00002702-\U000027B0"
        r"\U000024C2-\U0001F251"
        r"\u2600-\u26FF"
        r"\u2700-\u27BF]+",
        "", text
    )

    # Collapse multiple whitespaces
    text = re.sub(r"\s+", " ", text).strip()
    if not text:
        return "Done."

    # Casualize phrasing for Roman-script / English text
    if not _has_telugu_script(text):
        for pattern, repl in CASUAL_REPLACEMENTS:
            text = re.sub(pattern, repl, text, flags=re.IGNORECASE)

    # 9. Length truncation at sentence boundary if overly verbose
    if len(text) > max_chars:
        sentences = re.split(r"(?<=[.!?])\s+", text)
        truncated = []
        curr_len = 0
        for s in sentences:
            if curr_len + len(s) > max_chars and truncated:
                break
            truncated.append(s)
            curr_len += len(s) + 1
        
        result = " ".join(truncated).strip()
        if not result.endswith((".", "!", "?")):
            result += "."
        text = f"{result} I've placed the full details on your screen."

    return text


class VoiceService:
    def __init__(self):
        self.enabled = True
        secrets = load_secrets()
        self.elevenlabs_api_key = secrets.get("elevenlabs_api_key", "")
        self.engine_type = secrets.get("voice_engine", "edge_tts")
        self.groq_keys = secrets.get("groq_api_keys", [])
        if not self.groq_keys and secrets.get("groq_api_key"):
            self.groq_keys = [secrets.get("groq_api_key")]

    async def transcribe_bytes(self, audio_bytes: bytes, filename: str = "audio.ogg") -> str:
        """Transcribes raw audio bytes using Groq whisper-large-v3."""
        if not audio_bytes:
            return ""

        def _do_transcribe():
            try:
                from groq import Groq
            except ImportError:
                logger.warning("[VoicePipeline] stage=stt error=groq_not_installed")
                return ""

            for key in self.groq_keys:
                try:
                    client = Groq(api_key=key)
                    t0 = time.time()
                    transcription = client.audio.transcriptions.create(
                        file=(filename, audio_bytes),
                        model="whisper-large-v3"
                    )
                    dur_ms = (time.time() - t0) * 1000
                    txt = transcription.text.strip()
                    logger.info(f"[VoicePipeline] stage=stt bytes={len(audio_bytes)} duration_ms={dur_ms:.1f} status=ok")
                    return txt
                except Exception as e:
                    logger.warning(f"[VoicePipeline] stage=stt attempt_error={e}")
                    continue
            return ""

        return await asyncio.to_thread(_do_transcribe)

    async def speak(self, text: str):
        """Clean text, detect language, and route to the configured voice pipeline."""
        if not text or not self.enabled:
            return

        clean = sanitize_for_speech(text)
        if not clean:
            return

        await asyncio.to_thread(self._speak_sync, clean)

    @staticmethod
    def _casualize(text: str) -> str:
        """Loosen up formal phrasing so the TTS delivery sounds conversational."""
        for pattern, repl in CASUAL_REPLACEMENTS:
            text = re.sub(pattern, repl, text, flags=re.IGNORECASE)
        return text

    # ─────────────────────────────────────────────────────────────────
    # SYNC ROUTER: English / Tenglish -> Prabhat | Telugu Script -> Mohan
    # ─────────────────────────────────────────────────────────────────

    def _speak_sync(self, text: str):
        # Optional ElevenLabs override if configured
        if self.elevenlabs_api_key and self.engine_type == "elevenlabs":
            try:
                self._speak_elevenlabs(text)
                return
            except Exception as e:
                logger.warning(f"ElevenLabs error: {e}")

        # Choose voice according to pipeline
        if _has_telugu_script(text):
            voice, rate, pitch = VOICE_MOHAN, RATE_MOHAN, PITCH_MOHAN
            logger.info("Language detected: Telugu (Script) -> Route to Mohan")
        else:
            # English & Tenglish (Roman script)
            voice, rate, pitch = VOICE_PRABHAT, RATE_PRABHAT, PITCH_PRABHAT
            logger.info("Language detected: English / Tenglish -> Route to Prabhat")

        # Primary engine: Edge TTS (Free Neural)
        try:
            self._speak_edge_tts(text, voice, rate, pitch)
            return
        except Exception as e:
            logger.warning(f"Edge TTS ({voice}) failed: {e}, attempting offline fallback")

        # Offline fallback: SAPI5 pyttsx3 (Windows local only)
        try:
            self._speak_pyttsx3(text)
        except Exception as e:
            logger.warning(f"Offline pyttsx3 fallback failed: {e}")

    # ─────────────────────────────────────────────────────────────────
    # EDGE TTS
    # ─────────────────────────────────────────────────────────────────

    def _speak_edge_tts(self, text: str, voice: str, rate: str, pitch: str = "+0Hz"):
        import io
        import soundfile as sf
        import sounddevice as sd

        t0 = time.perf_counter()
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                loop = asyncio.new_event_loop()
                try:
                    audio_bytes = loop.run_until_complete(
                        self._generate_edge_tts_bytes(text, voice, rate, pitch)
                    )
                finally:
                    loop.close()

                if not audio_bytes:
                    raise RuntimeError("Edge TTS generated empty audio data")

                duration_ms = (time.perf_counter() - t0) * 1000
                logger.info(f"[VoicePipeline] stage=tts voice={voice} bytes={len(audio_bytes)} duration_ms={duration_ms:.1f} status=ok")

                try:
                    audio_data, sr = sf.read(io.BytesIO(audio_bytes), dtype="float32")
                    silence_n = int(SILENCE_PRE_ROLL_SEC * sr)
                    if audio_data.ndim == 1:
                        silence = np.zeros(silence_n, dtype=np.float32)
                    else:
                        silence = np.zeros((silence_n, audio_data.shape[1]), dtype=np.float32)

                    sd.play(np.concatenate([silence, audio_data], axis=0), samplerate=sr)
                    sd.wait()
                except Exception as play_err:
                    # In headless environments, local audio output is safely bypassed
                    logger.debug(f"[VoicePipeline] Local audio playback skipped (headless environment): {play_err}")
                return

            except Exception as e:
                logger.warning(f"[VoicePipeline] stage=tts attempt={attempt}/{MAX_RETRIES} error={e}")
                if attempt < MAX_RETRIES:
                    time.sleep(RETRY_DELAY)
                else:
                    raise

    async def generate_tts_audio(self, text: str) -> bytes:
        """Headless-safe Edge TTS MP3 byte synthesis for streaming HUD sentence chunks."""
        clean = sanitize_for_speech(text or "", max_chars=400)
        if not clean:
            return b""
        if _has_telugu_script(clean):
            voice, rate, pitch = VOICE_MOHAN, RATE_MOHAN, PITCH_MOHAN
        else:
            voice, rate, pitch = VOICE_PRABHAT, RATE_PRABHAT, PITCH_PRABHAT
        return await self._generate_edge_tts_bytes(clean, voice, rate, pitch)

    async def _generate_edge_tts_bytes(self, text: str, voice: str, rate: str, pitch: str) -> bytes:
        import edge_tts
        clean = sanitize_for_speech(text)
        comm = edge_tts.Communicate(clean, voice, rate=rate, pitch=pitch)
        buffer = bytearray()
        async for chunk in comm.stream():
            if chunk["type"] == "audio":
                buffer.extend(chunk["data"])
        return bytes(buffer)

    # ─────────────────────────────────────────────────────────────────
    # ELEVENLABS
    # ─────────────────────────────────────────────────────────────────

    def _speak_elevenlabs(self, text: str):
        import requests
        url = "https://api.elevenlabs.io/v1/text-to-speech/21m00Tcm4TlvDq8ikWAM"
        headers = {
            "Accept": "audio/mpeg",
            "Content-Type": "application/json",
            "xi-api-key": self.elevenlabs_api_key,
        }
        payload = {
            "text": text,
            "model_id": "eleven_multilingual_v2",
            "voice_settings": {
                "stability": 0.35,
                "similarity_boost": 0.75,
                "style": 0.45,
                "use_speaker_boost": True,
            },
        }
        t0 = time.perf_counter()
        response = requests.post(url, json=payload, headers=headers, timeout=10.0)
        response.raise_for_status()
        duration_ms = (time.perf_counter() - t0) * 1000
        logger.info(f"[VoicePipeline] stage=tts engine=elevenlabs bytes={len(response.content)} duration_ms={duration_ms:.1f} status=ok")
        self._play_audio_bytes(response.content)

    def _play_audio_bytes(self, audio_bytes: bytes):
        import soundfile as sf
        import sounddevice as sd
        import io
        try:
            audio_data, sr = sf.read(io.BytesIO(audio_bytes), dtype="float32")
            silence_n = int(SILENCE_PRE_ROLL_SEC * sr)
            if audio_data.ndim == 1:
                silence = np.zeros(silence_n, dtype=np.float32)
            else:
                silence = np.zeros((silence_n, audio_data.shape[1]), dtype=np.float32)
            sd.play(np.concatenate([silence, audio_data], axis=0), samplerate=sr)
            sd.wait()
        except Exception as play_err:
            logger.debug(f"[VoicePipeline] Local playback skipped (headless environment): {play_err}")

    # ─────────────────────────────────────────────────────────────────
    # OFFLINE SAPI5 (pyttsx3) FALLBACK
    # ─────────────────────────────────────────────────────────────────

    def _speak_pyttsx3(self, text: str):
        import pyttsx3
        _com = None
        try:
            import pythoncom
            pythoncom.CoInitialize()
            _com = pythoncom
        except Exception:
            pass
        try:
            engine = pyttsx3.init()
            voices = engine.getProperty("voices")
            male = next(
                (v for v in voices if "david" in v.name.lower() or "mark" in v.name.lower()),
                voices[0] if voices else None,
            )
            if male:
                engine.setProperty("voice", male.id)
            engine.setProperty("rate", 160)
            engine.say(text)
            engine.runAndWait()
        finally:
            if _com:
                _com.CoUninitialize()

    # ─────────────────────────────────────────────────────────────────
    # MICROPHONE INPUT (Fallback for local Python execution)
    # ─────────────────────────────────────────────────────────────────

    async def listen(self, lang_hint: str = "en-IN") -> str:
        try:
            import speech_recognition as sr
            recognizer = sr.Recognizer()
            with sr.Microphone() as source:
                logger.info("Listening for voice input...")
                audio = recognizer.listen(source, timeout=5, phrase_time_limit=10)
                text = recognizer.recognize_google(audio, language=lang_hint)
                logger.info("[VoicePipeline] stage=stt engine=google_fallback status=ok")
                return text
        except Exception as e:
            logger.warning(f"[VoicePipeline] stage=stt error={e}")
            return ""
