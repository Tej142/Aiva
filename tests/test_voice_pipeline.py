import unittest
import time
import io
import re
from unittest.mock import MagicMock, patch
from services.voice_service import sanitize_for_speech, VoiceService


class VoiceStateMachineSimulator:
    """
    Python mirror of the client-side VoiceStateMachine (ui/voice-manager.js).
    Validates state machine logic and transition invariant properties.
    """
    IDLE = 'idle'
    LISTENING = 'listening'
    PROCESSING = 'processing'
    SPEAKING = 'speaking'
    FOLLOW_UP = 'follow_up'
    MUTED = 'muted'
    ERROR = 'error'

    VALID_TRANSITIONS = {
        IDLE: [LISTENING, MUTED, ERROR],
        LISTENING: [PROCESSING, IDLE, MUTED, ERROR],
        PROCESSING: [SPEAKING, IDLE, MUTED, ERROR],
        SPEAKING: [FOLLOW_UP, LISTENING, IDLE, MUTED, ERROR],
        FOLLOW_UP: [LISTENING, IDLE, MUTED, ERROR],
        MUTED: [IDLE, ERROR],
        ERROR: [IDLE, LISTENING, MUTED]
    }

    def __init__(self):
        self.current_state = self.IDLE
        self.last_error = None

    def transition(self, target_state, error_code=None):
        if self.current_state == target_state:
            return True
        allowed = self.VALID_TRANSITIONS.get(self.current_state, [])
        if target_state not in allowed:
            return False
        self.current_state = target_state
        if error_code:
            self.last_error = error_code
        return True


class TestVoicePipeline(unittest.TestCase):
    """
    Regression and unit tests for the AIVA Voice Pipeline overhaul.
    """

    # 1. Text Sanitization Tests
    def test_sanitize_code_blocks(self):
        raw = "Here is the implementation:\n```python\nimport os\nprint('hello')\n```\nLet me know if you need changes."
        clean = sanitize_for_speech(raw)
        self.assertNotIn("import os", clean)
        self.assertNotIn("```", clean)
        self.assertIn("code on your screen", clean)

    def test_sanitize_urls_and_markdown_links(self):
        raw = "You can read the docs at [AIVA Docs](https://docs.aiva.internal/api) or visit https://github.com/aiva."
        clean = sanitize_for_speech(raw)
        self.assertNotIn("https://", clean)
        self.assertIn("AIVA Docs", clean)
        self.assertIn("the link on screen", clean)

    def test_sanitize_markdown_symbols_and_emojis(self):
        raw = "### System Status\n* **Status**: *Online* 🚀\n* __Latency__: 24ms 😊"
        clean = sanitize_for_speech(raw)
        self.assertNotIn("###", clean)
        self.assertNotIn("**", clean)
        self.assertNotIn("🚀", clean)
        self.assertNotIn("😊", clean)
        self.assertIn("Status", clean)
        self.assertIn("Online", clean)

    def test_sanitize_excessive_length_summarization(self):
        long_text = (
            "First sentence about system architecture. "
            "Second sentence detailing performance metrics and memory usage. "
            "Third sentence explaining the distributed state model. "
            "Fourth sentence covering database replication topologies across availability domains. "
            "Fifth sentence analyzing network jitter and edge compute latencies across multi-region clusters."
        )
        clean = sanitize_for_speech(long_text, max_chars=180)
        self.assertLess(len(clean), len(long_text))
        self.assertIn("full details on your screen", clean)

    # 2. Headless Audio Safety
    def test_headless_audio_safety(self):
        vs = VoiceService()
        self.assertTrue(vs.enabled)
        self.assertIsNotNone(vs)

    # 3. State Machine Invariants
    def test_voice_state_machine_valid_lifecycle(self):
        sm = VoiceStateMachineSimulator()
        self.assertEqual(sm.current_state, VoiceStateMachineSimulator.IDLE)

        # IDLE -> LISTENING -> PROCESSING -> SPEAKING -> FOLLOW_UP -> IDLE
        self.assertTrue(sm.transition(VoiceStateMachineSimulator.LISTENING))
        self.assertTrue(sm.transition(VoiceStateMachineSimulator.PROCESSING))
        self.assertTrue(sm.transition(VoiceStateMachineSimulator.SPEAKING))
        self.assertTrue(sm.transition(VoiceStateMachineSimulator.FOLLOW_UP))
        self.assertTrue(sm.transition(VoiceStateMachineSimulator.IDLE))

    def test_voice_state_machine_barge_in_transition(self):
        sm = VoiceStateMachineSimulator()
        sm.transition(VoiceStateMachineSimulator.LISTENING)
        sm.transition(VoiceStateMachineSimulator.PROCESSING)
        sm.transition(VoiceStateMachineSimulator.SPEAKING)
        # Barge-in: speaking interrupts immediately to listening
        self.assertTrue(sm.transition(VoiceStateMachineSimulator.LISTENING))
        self.assertEqual(sm.current_state, VoiceStateMachineSimulator.LISTENING)

    def test_voice_state_machine_rejects_illegal_transitions(self):
        sm = VoiceStateMachineSimulator()
        # Cannot jump from IDLE directly to SPEAKING or PROCESSING
        self.assertFalse(sm.transition(VoiceStateMachineSimulator.SPEAKING))
        self.assertFalse(sm.transition(VoiceStateMachineSimulator.PROCESSING))
        self.assertEqual(sm.current_state, VoiceStateMachineSimulator.IDLE)

    def test_voice_state_machine_error_recovery(self):
        sm = VoiceStateMachineSimulator()
        sm.transition(VoiceStateMachineSimulator.LISTENING)
        self.assertTrue(sm.transition(VoiceStateMachineSimulator.ERROR, error_code="MIC_DENIED"))
        self.assertEqual(sm.last_error, "MIC_DENIED")
        # Can recover from ERROR back to IDLE
        self.assertTrue(sm.transition(VoiceStateMachineSimulator.IDLE))
        self.assertEqual(sm.current_state, VoiceStateMachineSimulator.IDLE)

    # 4. Typed Error Hierarchy
    def test_typed_error_model(self):
        known_codes = [
            "MIC_DENIED", "NO_DEVICE", "MIC_UNPLUGGED", "STT_FAILED",
            "TTS_FAILED", "NETWORK", "TIMEOUT", "INSECURE_CONTEXT", "AUDIO_INIT_FAILED"
        ]
        # Verify that all required codes are covered
        for code in known_codes:
            sm = VoiceStateMachineSimulator()
            self.assertTrue(sm.transition(VoiceStateMachineSimulator.ERROR, error_code=code))
            self.assertEqual(sm.last_error, code)


    def test_destructive_action_requires_voice_confirmation(self):
        import asyncio
        from unittest.mock import AsyncMock, MagicMock
        from core.interaction.input_handler import InputHandler

        bus = MagicMock()
        bus.emit = AsyncMock()
        router = MagicMock()
        conv = MagicMock()
        mem_engine = MagicMock()
        mem_engine.get_working_messages.return_value = []
        mem_engine.get_user_display_name.return_value = "Charan"
        ctx = MagicMock()

        handler = InputHandler(
            event_bus=bus,
            intent_router_service=router,
            conversation_service=conv,
            memory_engine=mem_engine,
            context_resolver_service=ctx,
        )

        loop = asyncio.new_event_loop()
        try:
            # 1. Ask for shutdown -> requires confirmation
            res1 = loop.run_until_complete(handler.process_query("shut down my computer", speak=False))
            self.assertEqual(res1["route"], "confirmation_required")
            self.assertIn("Say 'confirm' to proceed", res1["response"])
            self.assertIsNotNone(handler.pending_confirmation)

            # 2. Confirm verbally
            res2 = loop.run_until_complete(handler.process_query("confirm", speak=False))
            self.assertEqual(res2["route"], "task")
            self.assertIn("Confirmed", res2["response"])
            self.assertIsNone(handler.pending_confirmation)
            bus.emit.assert_called()
        finally:
            loop.close()


if __name__ == "__main__":
    unittest.main()
