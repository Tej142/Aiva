"""
CognitionEngine — AIVA's meta-reasoning layer.

Responsibilities:
  - Pre-flight ambiguity detection: catches under-specified goals before they
    hit the workflow planner and generates clarifying questions.
  - Confidence gating: if intent classification confidence is low it can
    request a second-pass classification with richer context.
  - Chain-of-thought reflection: for complex multi-step goals it produces an
    internal reasoning trace that is injected into the planner prompt.
  - Goal decomposition hints: splits compound goals into atomic sub-goals
    so the planner builds cleaner DAG graphs.
"""

import re
from core.shared.logger import get_logger

logger = get_logger("CognitionEngine")

# ---------------------------------------------------------------------------
# Signals that a goal is underspecified and needs clarification
# ---------------------------------------------------------------------------
AMBIGUOUS_PATTERNS = [
    (r"\bsend (it|this|that)\b", "Send what exactly? Who should I send it to?"),
    (r"\bopen (it|this|that)\b", "Open which application or file?"),
    (r"\bplay (it|this|that)\b", "Play which song or media?"),
    (r"\bsearch (it|this|that)\b", "Search for what exactly?"),
    (r"\btype (it|this|that)\b", "Type what text?"),
    (r"\bdo (it|that) again\b", "Repeat which action?"),
    (r"\bclose (it|this|that)\b", "Close which application or window?"),
]

# Intents that benefit from chain-of-thought decomposition
COMPLEX_INTENT_KEYWORDS = [
    " and then ", " after that ", " also ", " followed by ",
    " then ", " first ", " next ", " finally ",
]


class CognitionEngine:
    """
    Meta-reasoning layer between InputHandler and PlannerAgent.
    Called optionally by the kernel before dispatching to the planner.
    """

    def __init__(self):
        logger.info("CognitionEngine initialized")

    # ------------------------------------------------------------------
    # PUBLIC API
    # ------------------------------------------------------------------

    def analyze(self, user_input: str, intent: str) -> dict:
        """
        Analyze a user goal before handing it to the planner.

        Returns a dict:
          {
            "proceed":     bool,   # True = hand off to planner as-is
            "clarify":     str,    # Non-empty when a question should be asked
            "decomposed":  list,   # Optional list of sub-goal strings
            "cot_hint":    str,    # Chain-of-thought reasoning hint for planner
          }
        """
        result = {
            "proceed": True,
            "clarify": "",
            "decomposed": [],
            "cot_hint": "",
        }

        # 1. Ambiguity check
        clarify_msg = self._check_ambiguity(user_input)
        if clarify_msg:
            logger.info(f"Ambiguous goal detected: '{user_input}' → asking: {clarify_msg}")
            result["proceed"] = False
            result["clarify"] = clarify_msg
            return result

        # 2. Complexity check → decompose compound goals
        if self._is_complex(user_input):
            sub_goals = self._decompose(user_input)
            if sub_goals:
                result["decomposed"] = sub_goals
                result["cot_hint"] = self._build_cot_hint(sub_goals)
                logger.info(f"Complex goal decomposed into {len(sub_goals)} sub-goals")

        return result

    def should_clarify(self, user_input: str) -> str:
        """
        Shorthand: returns clarification question string, or empty string.
        """
        return self._check_ambiguity(user_input)

    # ------------------------------------------------------------------
    # PRIVATE HELPERS
    # ------------------------------------------------------------------

    def _check_ambiguity(self, text: str) -> str:
        lower = text.lower().strip()
        for pattern, question in AMBIGUOUS_PATTERNS:
            if re.search(pattern, lower):
                return question
        return ""

    def _is_complex(self, text: str) -> bool:
        lower = text.lower()
        return any(kw in lower for kw in COMPLEX_INTENT_KEYWORDS)

    def _decompose(self, text: str) -> list:
        """
        Naively split compound goals on common conjunctions.
        Returns a list of trimmed sub-goal strings.
        """
        # Split on 'and then', 'then', 'after that', 'also', 'followed by', 'next', 'finally'
        parts = re.split(
            r"\b(?:and then|after that|followed by|then|also|next|finally|first)\b",
            text,
            flags=re.IGNORECASE,
        )
        sub_goals = [p.strip().strip(",. ") for p in parts if p.strip()]
        return sub_goals if len(sub_goals) > 1 else []

    def _build_cot_hint(self, sub_goals: list) -> str:
        steps = "\n".join(f"  {i+1}. {g}" for i, g in enumerate(sub_goals))
        return (
            f"This is a compound goal. Execute in order:\n{steps}\n"
            "Generate a workflow node for each sub-goal in dependency order."
        )
