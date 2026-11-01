"""
OutcomeReporterService â€” Generates and speaks smart task completion feedback.

After every workflow finishes (success, partial, or failure), this service:
  1. Collects the real per-node results (what worked, what failed, why)
  2. Passes them to a Groq LLM with a focused prompt
  3. Gets back a concise, natural-language outcome summary
  4. Speaks it via VoiceService

The outcome is never hardcoded â€” the LLM decides how to phrase it based on
the actual results, so partial successes and specific error reasons are
communicated naturally, just like a human assistant would.
"""

import asyncio
from core.shared.config_loader import load_secrets
from core.shared.logger import get_logger

logger = get_logger("OutcomeReporterService")

# System prompt for the outcome narrator
_OUTCOME_SYSTEM_PROMPT = """
You are AIVA, a personal AI assistant speaking to the user after completing (or attempting) a task on their PC.

Your job: Summarize what just happened in 1-2 natural sentences.

Rules:
- Be honest: if something failed, say so clearly but calmly.
- Be specific: mention which step worked or which step failed and why (use the info given).
- Never say "I cannot" or "I am unable" â€” say what DID happen instead.
- Match the user's language style: if they spoke in Tanglish, respond in Tanglish.
- No emojis, no markdown, no bullet points. Pure spoken English or Tanglish.
- If everything succeeded, confirm it confidently and briefly.
- If partially done, say what was done and what wasn't.
- Keep it short â€” this is spoken aloud.
"""


class OutcomeReporterService:
    """
    Generates spoken natural-language task completion feedback.
    Bound to VoiceService and ConversationMemoryService after kernel starts.
    """

    def __init__(self):
        secrets = load_secrets()
        self.api_keys = secrets.get("groq_api_keys", [])
        if not self.api_keys and secrets.get("groq_api_key"):
            self.api_keys = [secrets.get("groq_api_key")]
        self.gemini_api_key = secrets.get("gemini_api_key", "")
        self.current_key_idx = 0
        self.voice_service = None                   # set by kernel after init
        self.conversation_memory_service = None     # set by kernel after init
        self.wake_word_service = None               # set by kernel after init
        logger.info("OutcomeReporterService initialized")

    # ------------------------------------------------------------------
    # MAIN ENTRY POINT â€” called by kernel after workflow execution
    # ------------------------------------------------------------------

    async def report(
        self,
        goal: str,
        node_results: list,          # [{"node_id", "action", "status", "error"?}, ...]
        user_language_hint: str = "english"
    ):
        """
        Generate and speak a natural outcome summary.

        Args:
            goal:               The original user goal string.
            node_results:       List of per-node outcome dicts from WorkflowEngine.
            user_language_hint: "english" or "tanglish" â€” mirrors user's style.
        """
        if not node_results:
            return

        outcome_text = await self._generate_outcome(goal, node_results, user_language_hint)
        if not outcome_text:
            return

        logger.info(f"[OutcomeReporter] Speaking outcome: {outcome_text[:80]}...")
        print(f"\nAIVA: {outcome_text}\n")

        # Update conversation memory so AIVA remembers what happened
        if self.conversation_memory_service:
            self.conversation_memory_service.add_turn(goal, outcome_text)

        # Speak outcome aloud
        if self.voice_service:
            await self.voice_service.speak(outcome_text)

        # Trigger follow-up listening window so user can issue next command without saying wake word again
        if self.wake_word_service and getattr(self.wake_word_service, "running", False):
            self.wake_word_service.trigger_followup(timeout=7.0)

    # ------------------------------------------------------------------
    # LLM OUTCOME GENERATION
    # ------------------------------------------------------------------

    async def _generate_outcome(
        self,
        goal: str,
        node_results: list,
        lang_hint: str
    ) -> str:
        """Ask the LLM to narrate what happened based on real node results."""

        # Build a compact result summary for the prompt
        result_lines = []
        all_success = True
        for r in node_results:
            nid    = r.get("node_id", "step")
            action = r.get("action", "")
            status = r.get("status", "unknown")
            error  = r.get("error", "")
            if status != "completed":
                all_success = False
            line = f"- {nid} ({action}): {status}"
            if error:
                line += f" â€” reason: {error}"
            result_lines.append(line)

        result_block = "\n".join(result_lines)

        lang_instruction = (
            "Respond in casual Tanglish (Romanized Telugu-English mix)."
            if "tanglish" in lang_hint.lower()
            else "Respond in concise English."
        )

        user_prompt = (
            f"User asked AIVA to: \"{goal}\"\n\n"
            f"Execution results:\n{result_block}\n\n"
            f"{lang_instruction}"
        )

        # Try Groq first
        outcome = await self._try_groq(user_prompt)
        if outcome:
            return outcome

        # Fallback to Gemini
        outcome = await self._try_gemini(user_prompt)
        if outcome:
            return outcome

        # Hard fallback: compose a basic message without LLM
        return self._compose_fallback(goal, node_results, all_success)

    async def _try_groq(self, user_prompt: str) -> str:
        try:
            from groq import AsyncGroq
            for idx in range(len(self.api_keys)):
                k_idx = (self.current_key_idx + idx) % len(self.api_keys)
                key = self.api_keys[k_idx]
                client = AsyncGroq(api_key=key)
                for model in ["openai/gpt-oss-20b", "qwen/qwen3.8-27b"]:
                    try:
                        resp = await client.chat.completions.create(
                            model=model,
                            messages=[
                                {"role": "system", "content": _OUTCOME_SYSTEM_PROMPT},
                                {"role": "user",   "content": user_prompt}
                            ],
                            temperature=0.5,
                            max_completion_tokens=120
                        )
                        text = resp.choices[0].message.content.strip()
                        if text:
                            self.current_key_idx = k_idx
                            return text
                    except Exception as e:
                        err = str(e)
                        if "429" in err or "rate_limit" in err:
                            break
                        logger.debug(f"Groq model {model} error: {e}")
        except Exception as e:
            logger.warning(f"OutcomeReporter Groq error: {e}")
        return ""

    async def _try_gemini(self, user_prompt: str) -> str:
        try:
            from google import genai
            client = genai.Client(api_key=self.gemini_api_key)
            prompt = f"{_OUTCOME_SYSTEM_PROMPT}\n\n{user_prompt}"
            resp = await asyncio.to_thread(
                client.models.generate_content,
                model="gemini-2.0-flash",
                contents=prompt
            )
            return resp.text.strip()
        except Exception as e:
            logger.warning(f"OutcomeReporter Gemini fallback error: {e}")
        return ""

    def _compose_fallback(self, goal: str, node_results: list, all_success: bool) -> str:
        """Minimal rule-based fallback if all LLMs fail."""
        total   = len(node_results)
        done    = sum(1 for r in node_results if r.get("status") == "completed")
        failed  = total - done

        if all_success:
            return f"Done. All {total} steps for '{goal}' completed successfully."
        elif done == 0:
            return f"Sorry, I wasn't able to complete '{goal}'. All {total} steps failed."
        else:
            return (
                f"Partially done. {done} of {total} steps completed for '{goal}', "
                f"but {failed} step{'s' if failed > 1 else ''} failed."
            )

