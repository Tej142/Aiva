import asyncio
from google import genai
from groq import AsyncGroq
from core.shared.config_loader import load_secrets
from core.shared.logger import get_logger
from core.memory.safety import parse_call_me_command

logger = get_logger("IntentClassifierService")


class IntentClassifierService:
    def __init__(self):
        secrets = load_secrets()
        self.gemini_keys = secrets.get("gemini_api_keys", [])
        if not self.gemini_keys and secrets.get("gemini_api_key"):
            self.gemini_keys = [secrets.get("gemini_api_key")]
        self.current_key_idx = 0
        self.groq_key = secrets.get("groq_api_key", "")
        self.groq_client = AsyncGroq(api_key=self.groq_key) if self.groq_key else None
        self.model = "gemini-3.5-flash-lite"
        self.valid_intents = {
            "conversation",
            "knowledge",
            "research",
            "action",
            "memory",
        }

    def _get_gemini_client(self, api_key: str):
        return genai.Client(api_key=api_key)

    @staticmethod
    def _normalize_intent(token: str) -> str | None:
        clean = token.strip(".,!?: ").lower()
        if clean in ("memory_write", "memory_read", "memory", "reminder"):
            return "memory"
        if clean in ("conversation", "knowledge", "research", "action"):
            return clean
        return None

    async def classify(
        self,
        user_input: str,
        conversation_context=None,
    ) -> str:
        clean_input = user_input.strip().lower()
        # Fast local heuristics
        if parse_call_me_command(user_input) is not None:
            return "memory"
        if any(
            clean_input.startswith(prefix)
            for prefix in (
                "remember ",
                "remember that ",
                "don't forget ",
                "dont forget ",
                "save this ",
                "keep in mind ",
                "remind me ",
                "forget ",
                "snooze ",
                "what do you know about",
                "what do you remember about",
                "what are my reminders",
                "list my reminders",
                "list reminders",
                "what is my ",
                "do you remember ",
                "who am i",
                "pause learning",
                "resume learning",
                "turn off memory",
                "turn on memory",
            )
        ):
            return "memory"

        if any(
            w in clean_input
            for w in (
                "open ",
                "launch ",
                "start ",
                "vethuku",
                "lo python",
                "lo youtube",
                "on my screen",
                "on the screen",
                "inspect screen",
                "read screen",
                "click on ",
                "click the ",
            )
        ):
            return "action"

        if conversation_context is None:
            conversation_context = []

        context_lines = []
        for turn in conversation_context[-3:]:
            if isinstance(turn, dict):
                if "role" in turn and "content" in turn:
                    prefix = "User" if turn["role"] == "user" else "AIVA"
                    context_lines.append(f"{prefix}: {turn['content']}")
                else:
                    if turn.get("user"):
                        context_lines.append(f"User: {turn['user']}")
                    if turn.get("aiva"):
                        context_lines.append(f"AIVA: {turn['aiva']}")

        context_text = "\n".join(context_lines)

        prompt = f"""
You are the intent classification engine for AIVA.
Classify the CURRENT USER MESSAGE (in English, Telugu, or Tanglish) into exactly ONE intent: conversation, knowledge, research, action, memory.
Use `memory` for remembering personal facts, forgetting, recalling personal info/entities, or reminders/snooze.

RECENT CONVERSATION:
{context_text}

CURRENT USER MESSAGE:
{user_input}

Return ONLY one intent word.
"""
        # Try Gemini Key Pool
        for idx in range(len(self.gemini_keys)):
            k_idx = (self.current_key_idx + idx) % len(self.gemini_keys)
            key = self.gemini_keys[k_idx]
            try:
                client = self._get_gemini_client(key)
                for g_model in ["gemini-3.5-flash-lite", "gemini-2.0-flash", "gemini-2.5-flash"]:
                    try:
                        response = await asyncio.to_thread(
                            client.models.generate_content,
                            model=g_model,
                            contents=prompt,
                        )
                        raw = response.text.strip().lower()
                        intent = next(
                            (
                                self._normalize_intent(word)
                                for word in raw.split()
                                if self._normalize_intent(word)
                            ),
                            None,
                        )
                        if intent:
                            self.current_key_idx = k_idx
                            logger.info(f"Classified intent via Gemini {g_model} (Key #{k_idx+1}): '{intent}'")
                            return intent
                    except Exception as model_e:
                        logger.debug(f"Gemini {g_model} error on Key #{k_idx+1}: {model_e}")
            except Exception as error:
                logger.warning(f"Gemini IntentClassifier on Key #{k_idx+1} error: {error}")

        # Fallback to Groq
        if self.groq_client:
            for model_cand in ["openai/gpt-oss-20b", "qwen/qwen3.8-27b"]:
                try:
                    res = await self.groq_client.chat.completions.create(
                        model=model_cand,
                        messages=[{"role": "user", "content": prompt}],
                        temperature=0,
                        max_completion_tokens=10,
                    )
                    intent = self._normalize_intent(res.choices[0].message.content)
                    if intent:
                        logger.info(f"Classified intent via Groq ({model_cand}): '{intent}'")
                        return intent
                except Exception as groq_err:
                    logger.warning(f"Groq IntentClassifier error with {model_cand}: {groq_err}")

        return "conversation"
