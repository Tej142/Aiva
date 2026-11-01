import os
import re
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
RETRY_DELAY = 1.0

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


class VoiceService:
    def __init__(self):
        self.enabled = True
        secrets = load_secrets()
        self.elevenlabs_api_key = secrets.get("elevenlabs_api_key", "")
        self.engine_type = secrets.get("voice_engine", "edge_tts")

    async def speak(self, text: str):
        """Clean text, detect language, and route to the configured voice pipeline."""
        if not text or not self.enabled:
            return

        # 1. Strip markdown symbols
        clean = re.sub(r"[*_#`~\[\]>]+", "", text)

        # 2. Strip ASCII emoticons
        clean = re.sub(r"[:;=8][\-o\*\']?[\)\]\(\[dDpP/\:\}\{@\|\\]", "", clean)
        clean = re.sub(r"[\)\]\(\[dDpP][\-o\*\']?[:;=8]", "", clean)
        clean = re.sub(r"\bxD\b|\bXD\b|\bhaha\b|\blol\b|\bhehe\b", "", clean, flags=re.IGNORECASE)

        # 3. Strip Unicode emojis
        clean = re.sub(
            r"[\U0001F600-\U0001F64F"
            r"\U0001F300-\U0001F5FF"
            r"\U0001F680-\U0001F6FF"
            r"\U0001F1E0-\U0001F1FF"
            r"\U00002702-\U000027B0"
            r"\U000024C2-\U0001F251"
            r"\u2600-\u26FF"
            r"\u2700-\u27BF]+",
            "", clean
        )

        clean = re.sub(r"\s+", " ", clean).strip()
        if not clean:
            return

        # 4. Casualize phrasing (only applies to Roman-script/English text;
        # Telugu script passes through untouched)
        if not _has_telugu_script(clean):
            clean = self._casualize(clean)

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

        # Offline fallback: SAPI5 pyttsx3
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

                audio_data, sr = sf.read(io.BytesIO(audio_bytes), dtype="float32")
                silence_n = int(SILENCE_PRE_ROLL_SEC * sr)
                if audio_data.ndim == 1:
                    silence = np.zeros(silence_n, dtype=np.float32)
                else:
                    silence = np.zeros((silence_n, audio_data.shape[1]), dtype=np.float32)

                sd.play(np.concatenate([silence, audio_data], axis=0), samplerate=sr)
                sd.wait()
                return

            except Exception as e:
                logger.warning(f"Edge TTS attempt {attempt}/{MAX_RETRIES}: {e}")
                if attempt < MAX_RETRIES:
                    import time
                    time.sleep(RETRY_DELAY)
                else:
                    raise

    async def _generate_edge_tts_bytes(self, text: str, voice: str, rate: str, pitch: str) -> bytes:
        import edge_tts
        comm = edge_tts.Communicate(text, voice, rate=rate, pitch=pitch)
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
            "model_id": "eleven_multilingual_v2",  # more natural/expressive prosody than monolingual_v1
            "voice_settings": {
                "stability": 0.35,       # lower = more natural variation, less "read aloud"
                "similarity_boost": 0.75,
                "style": 0.45,           # pushes toward expressive/casual delivery
                "use_speaker_boost": True,
            },
        }
        response = requests.post(url, json=payload, headers=headers)
        response.raise_for_status()
        self._play_audio_bytes(response.content)

    def _play_audio_bytes(self, audio_bytes: bytes):
        import soundfile as sf
        import sounddevice as sd
        import io
        audio_data, sr = sf.read(io.BytesIO(audio_bytes), dtype="float32")
        silence_n = int(SILENCE_PRE_ROLL_SEC * sr)
        if audio_data.ndim == 1:
            silence = np.zeros(silence_n, dtype=np.float32)
        else:
            silence = np.zeros((silence_n, audio_data.shape[1]), dtype=np.float32)
        sd.play(np.concatenate([silence, audio_data], axis=0), samplerate=sr)
        sd.wait()

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
                voices[0],
            )
            engine.setProperty("voice", male.id)
            engine.setProperty("rate", 160)
            engine.say(text)
            engine.runAndWait()
        finally:
            if _com:
                _com.CoUninitialize()

    # ─────────────────────────────────────────────────────────────────
    # MICROPHONE INPUT
    # ─────────────────────────────────────────────────────────────────

    async def listen(self, lang_hint: str = "en-IN") -> str:
        try:
            import speech_recognition as sr
            recognizer = sr.Recognizer()
            with sr.Microphone() as source:
                logger.info("Listening for voice input...")
                audio = recognizer.listen(source, timeout=5, phrase_time_limit=10)
                text = recognizer.recognize_google(audio, language=lang_hint)
                logger.info(f"Transcribed: '{text}'")
                return text
        except Exception as e:
            logger.warning(f"Voice recognition error: {e}")
            return ""