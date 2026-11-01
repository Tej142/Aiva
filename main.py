import sys
import asyncio
from core.runtime.kernel import AIVAKernel


async def main():
    # Flags for voice modes
    all_voice_mode = "--allvoice" in sys.argv or "-a" in sys.argv
    voice_mode = "--voice" in sys.argv or "-v" in sys.argv

    if all_voice_mode:
        print("\n" + "=" * 60)
        print("🎙️  AIVA ALL-VOICE INTERACTIVE MODE (Always Listening)")
        print("   Speak naturally at any time — no wake word needed!")
        print("=" * 60 + "\n")
    elif voice_mode:
        print("\n" + "=" * 60)
        print("🎙️  AIVA WAKE-WORD VOICE MODE ACTIVE")
        print("   Say 'Hey AIVA' to trigger commands")
        print("=" * 60 + "\n")

    kernel = AIVAKernel()
    await kernel.start(voice_mode=voice_mode, all_voice_mode=all_voice_mode)


if __name__ == "__main__":
    asyncio.run(main())