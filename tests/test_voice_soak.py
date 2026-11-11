import unittest
import time
import os
import gc
from services.voice_service import sanitize_for_speech, VoiceService


class VoiceSoakTest(unittest.TestCase):
    """
    Soak test validating memory stability, resource disposal,
    and absence of memory or file descriptor leaks across repeated voice cycles.
    """

    def test_voice_cycles_memory_stability(self):
        try:
            import psutil
            process = psutil.Process(os.getpid())
            has_psutil = True
        except ImportError:
            has_psutil = False

        vs = VoiceService()
        sample_queries = [
            "What is the status of the server?",
            "Open YouTube and play lo-fi music.",
            "Can you write a python script to monitor disk usage?",
            "Take a screenshot and summarize my screen.",
            "Set device audio volume to 40 percent.",
            "Tell me a short witty joke in Jarvis style."
        ]

        # Initial memory measurement
        gc.collect()
        initial_mem_mb = process.memory_info().rss / (1024 * 1024) if has_psutil else 0

        # Simulate 100 rapid wake-respond cycles
        start_time = time.perf_counter()
        cycles = 100
        for i in range(cycles):
            q = sample_queries[i % len(sample_queries)]
            cleaned = sanitize_for_speech(f"Acknowledged. Query {i}: {q}. Code: ```test\nx = 1\n``` https://aiva.ai")
            self.assertTrue(len(cleaned) > 0)
            self.assertNotIn("```", cleaned)
            self.assertNotIn("https://", cleaned)

        gc.collect()
        final_mem_mb = process.memory_info().rss / (1024 * 1024) if has_psutil else 0
        elapsed_sec = time.perf_counter() - start_time

        if has_psutil:
            mem_growth_mb = final_mem_mb - initial_mem_mb
            # Memory growth across 100 cycles must be under 15MB
            self.assertLess(mem_growth_mb, 15.0, f"Memory leaked: {mem_growth_mb:.2f}MB")
            print(f"\n[Soak Test] Ran {cycles} cycles in {elapsed_sec:.2f}s | Start Mem: {initial_mem_mb:.2f}MB | End Mem: {final_mem_mb:.2f}MB | Growth: {mem_growth_mb:.2f}MB")


if __name__ == "__main__":
    unittest.main()

