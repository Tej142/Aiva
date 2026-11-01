from groq import AsyncGroq
from core.shared.config_loader import load_secrets
from core.shared.logger import get_logger

logger = get_logger("ConversationService")

class ConversationService:
    def __init__(self):
        secrets = load_secrets()
        self.api_keys = secrets.get("groq_api_keys", [])
        if not self.api_keys and secrets.get("groq_api_key"):
            self.api_keys = [secrets.get("groq_api_key")]
        self.current_key_idx = 0
        self.model = "openai/gpt-oss-20b"
        self.fallback_models = [
            "qwen/qwen3.8-27b",
            "openai/gpt-oss-120b"
        ]

    def _get_client(self, api_key: str) -> AsyncGroq:
        return AsyncGroq(api_key=api_key)

    async def respond(
        self,
        user_input: str,
        conversation_history=None,
        user_context=None
    ) -> str:
        if conversation_history is None:
            conversation_history = []
        if user_context is None:
            user_context = "No persistent user information stored."

        system_prompt = f"""
You are AIVA — Charan's personal AI best friend, not just an assistant. You know him, you genuinely care, and you talk like a close friend.

What you currently know about him:
{user_context}

How to be his friend:
1. Be warm, real, and casual — like chatting with a close friend, not a helpdesk bot.
2. MIRROR his language exactly:
   - Tanglish/Romanized Telugu ("ela unnav", "em chesav", "baaga cheppav") → reply in warm natural Tanglish.
     Example: User says "ela unnav" → You say "ha bro, bagunnanu! nuvvu ela unnav?"
   - English → natural friendly English.
   - NEVER output Telugu Unicode/script — it breaks voice output.
3. Use what you remember about him naturally — like a friend who actually paid attention.
   If you know his name, use it occasionally. If you know he likes something, mention it when relevant.
4. Keep it SHORT — 1 to 2 sentences usually. No lectures. No padding.
5. If he asks you to do something on his PC, confirm warmly: "sure, on it!" or "opening that for you now."
6. If he sounds stressed, tired, or upset, briefly acknowledge it before helping — friends notice these things.
7. Never mention APIs, models, agents, routing, or memory files.
8. No emojis — this is spoken aloud.
"""
        messages = [{"role": "system", "content": system_prompt}]
        messages.extend(conversation_history[-8:])
        messages.append({"role": "user", "content": user_input})


        # Try across all available Groq keys in pool
        for idx in range(len(self.api_keys)):
            key_index = (self.current_key_idx + idx) % len(self.api_keys)
            key = self.api_keys[key_index]
            client = self._get_client(key)

            for model_candidate in [self.model] + self.fallback_models:
                try:
                    response = await client.chat.completions.create(
                        model=model_candidate,
                        messages=messages,
                        temperature=0.6,
                        max_completion_tokens=250
                    )
                    self.current_key_idx = key_index
                    return response.choices[0].message.content
                except Exception as error:
                    error_str = str(error)
                    if "429" in error_str or "rate_limit" in error_str or "401" in error_str:
                        logger.warning(f"Groq Key #{key_index + 1} rate limited. Rotating to next key...")
                        break  # Break to next key in pool
                    logger.warning(f"Error with model {model_candidate} on Key #{key_index + 1}: {error}")

        # Fallback to Gemini if all Groq keys fail
        try:
            from google import genai
            secrets = load_secrets()
            gemini_client = genai.Client(api_key=secrets.get("gemini_api_key"))
            prompt_text = f"{system_prompt}\n\nUSER:\n{user_input}\n\nAIVA:"
            resp = gemini_client.models.generate_content(
                model="gemini-2.0-flash",
                contents=prompt_text
            )
            return resp.text.strip()
        except Exception as e:
            logger.error(f"Gemini fallback failed: {e}")

        return "I couldn't generate a response right now."

    async def classify_intent(self, user_input: str) -> str:
        valid_intents = {"conversation", "knowledge", "research", "action"}
        system_prompt = """Classify the user's message (in English, Telugu, or Tanglish) into exactly ONE category: conversation, knowledge, research, action.

conversation: Casual chat, feelings, opinions, jokes, greetings ("hi", "ela vunnav", "à°¨à°®à°¸à±à°•à°¾à°°à°‚"), everyday interaction.
knowledge: Questions asking for explanations, facts, definitions, educational info.
research: Explicit requests for investigation, current info, web research.
action: Requests to open, close, launch, play, navigate, adjust volume, take screenshot, or control something (e.g. "open youtube", "volume penchu", "notepad open cheyyi").

Return ONLY one word from the list."""

        for model_candidate in [self.model] + self.fallback_models:
            try:
                response = await self.client.chat.completions.create(
                    model=model_candidate,
                    messages=[
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": user_input}
                    ],
                    temperature=0,
                    max_completion_tokens=10
                )
                intent = response.choices[0].message.content.strip().lower()
                if intent in valid_intents:
                    return intent
            except Exception as error:
                logger.warning(f"Intent Classification Error with model {model_candidate}: {error}")

        return "conversation"
