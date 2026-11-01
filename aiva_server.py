import asyncio
import re
import sys
import threading

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

import os
import json
from flask import Flask, request, jsonify, send_from_directory
from core.runtime.kernel import AIVAKernel

UI_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ui")
app = Flask(__name__, static_folder=UI_DIR)

# =====================================================================
# GLOBAL KERNEL & ASYNC THREAD RUNNER
# =====================================================================
kernel = None
kernel_loop = None
kernel_thread = None


def _clean_for_speech(text: str) -> str:
    """Clean markdown, emoji, and formatting so Alexa speaks naturally."""
    if not text:
        return "Done."
    clean = re.sub(r"[*_#`~\[\]>]+", "", text)
    clean = re.sub(
        r"[\U0001F600-\U0001F64F"
        r"\U0001F300-\U0001F5FF"
        r"\U0001F680-\U0001F6FF"
        r"\U0001F1E0-\U0001F1FF"
        r"\u2600-\u26FF\u2700-\u27BF]+",
        "",
        clean,
    )
    clean = re.sub(r"\s+", " ", clean).strip()
    return clean or "Done."


def build_alexa_response(speech_text: str, should_end_session: bool = False, reprompt_text: str = None) -> dict:
    """Constructs a valid Alexa Skills Kit JSON response."""
    clean_speech = _clean_for_speech(speech_text)
    resp = {
        "version": "1.0",
        "response": {
            "outputSpeech": {
                "type": "PlainText",
                "text": clean_speech
            },
            "shouldEndSession": should_end_session
        }
    }
    if not should_end_session and reprompt_text:
        resp["response"]["reprompt"] = {
            "outputSpeech": {
                "type": "PlainText",
                "text": _clean_for_speech(reprompt_text)
            }
        }
    return resp


def run_async(coro, timeout: float = 25.0):
    """Executes a coroutine on the background AIVA kernel event loop."""
    if kernel_loop is None or not kernel_loop.is_running():
        raise RuntimeError("AIVA Kernel event loop is not running.")
    future = asyncio.run_coroutine_threadsafe(coro, kernel_loop)
    return future.result(timeout=timeout)



# =====================================================================
# STATIC WEB UI SERVING
# =====================================================================
@app.route("/", methods=["GET"])
def index():
    """Serve the AIVA HUD Web UI."""
    return send_from_directory(UI_DIR, "index.html")


@app.route("/<path:filename>", methods=["GET"])
def static_files(filename):
    """Serve CSS, JS, and asset files."""
    return send_from_directory(UI_DIR, filename)


# =====================================================================
# REST APIS FOR WEB UI HUD & CONTROLS
# =====================================================================
@app.route("/api/chat", methods=["POST"])
def api_chat():
    """Processes queries from the Web UI HUD."""
    data = request.json or {}
    msg = data.get("message") or data.get("query") or ""

    if not msg:
        return jsonify({"status": "error", "error": "Missing message"}), 400

    print(f"\n[Web UI Chat Query]: {msg}")
    try:
        # Run through AIVA's intelligent pipeline (Intent Classifier -> Router -> DAG / Tool -> Prabhat TTS)
        result = run_async(kernel.input_handler.process_query(msg, speak=True))
        
        response_data = {
            "status": "success",
            "query": msg,
            "response": result.get("response", "Done."),
            "route": result.get("route", "general"),
            "should_end_session": result.get("should_end_session", False)
        }

        # Check if a recent screenshot was taken
        screenshot_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "storage", "last_screenshot.png")
        if os.path.exists(screenshot_path) and ("screen" in msg.lower() or "shot" in msg.lower()):
            import base64
            with open(screenshot_path, "rb") as img_file:
                response_data["screenshot_base64"] = base64.b64encode(img_file.read()).decode("utf-8")

        return jsonify(response_data)
    except Exception as e:
        print(f"[Web UI Chat Error]: {e}")
        return jsonify({"status": "error", "error": str(e)}), 500


@app.route("/api/status", methods=["GET"])
def api_status():
    """System health, key pool count, and kernel status."""
    return jsonify({
        "status": "online",
        "kernel_running": kernel is not None and kernel.running,
        "key_count": 17,
        "pools": {
            "groq": 7,
            "gemini": 5,
            "mistral": 5
        },
        "voice_engine": "Microsoft Edge Neural (en-IN-PrabhatNeural)"
    })


@app.route("/api/memory", methods=["GET"])
def api_memory():
    """Fetches user memory and learned task pattern recipes."""
    user_mem = {}
    task_patterns = {}

    if kernel:
        try:
            if hasattr(kernel, "user_memory_service") and kernel.user_memory_service:
                user_mem = kernel.user_memory_service.get_all()
        except Exception:
            pass

        try:
            if hasattr(kernel, "task_pattern_service") and kernel.task_pattern_service:
                task_patterns = kernel.task_pattern_service.patterns
        except Exception:
            pass

    # Fallback to direct file loading if kernel services were empty
    base_dir = os.path.dirname(os.path.abspath(__file__))
    if not user_mem:
        for p in [os.path.join(base_dir, "memory", "user_memory.json"), os.path.join(base_dir, "storage", "user_memory.json")]:
            if os.path.exists(p):
                try:
                    with open(p, "r", encoding="utf-8") as f:
                        user_mem = json.load(f)
                    break
                except Exception:
                    pass

    if not task_patterns:
        for p in [os.path.join(base_dir, "memory", "task_patterns.json"), os.path.join(base_dir, "storage", "task_patterns.json")]:
            if os.path.exists(p):
                try:
                    with open(p, "r", encoding="utf-8") as f:
                        task_patterns = json.load(f)
                    break
                except Exception:
                    pass

    return jsonify({
        "user_memory": user_mem,
        "task_patterns": task_patterns
    })


# =====================================================================
# VOICE MODE CONTROL API (used by the Web HUD toggles)
# =====================================================================
@app.route("/api/voice/status", methods=["GET"])
def api_voice_status():
    """Returns the current voice mode and dialogue state."""
    if not kernel or not hasattr(kernel, "wake_word_service"):
        return jsonify({"running": False, "mode": "off", "state": "off"})
    wws = kernel.wake_word_service
    mode = "off"
    state = "idle"
    if wws.running:
        mode = getattr(wws, "mode", "adaptive")
        state = getattr(wws, "state", "standby")
    return jsonify({"running": wws.running, "mode": mode, "state": state})


@app.route("/api/voice/control", methods=["POST"])
def api_voice_control():
    """
    Control the WakeWordService from the Web HUD.

    Expected JSON body:
        { "action": "start_adaptive" }  — start adaptive conversational runtime (dynamic turn-taking)
        { "action": "start_wakeword" }  — start in strict wake-word mode ("Hey AIVA")
        { "action": "start_allvoice" }  — start in continuous all-voice mode
        { "action": "stop" }            — disable voice listening entirely
    """
    if not kernel or not hasattr(kernel, "wake_word_service"):
        return jsonify({"status": "error", "message": "Kernel not ready"}), 503

    data = request.json or {}
    action = data.get("action", "").strip().lower()
    wws = kernel.wake_word_service

    if action == "stop":
        wws.stop()
        return jsonify({"status": "ok", "mode": "off", "state": "off"})

    elif action in ("start_adaptive", "start_wakeword", "start_allvoice"):
        target_mode = "adaptive"
        if action == "start_wakeword":
            target_mode = "wakeword"
        elif action == "start_allvoice":
            target_mode = "allvoice"

        if wws.running:
            if getattr(wws, "mode", "") == target_mode:
                return jsonify({"status": "already_running", "mode": target_mode, "state": getattr(wws, "state", "standby")})
            wws.stop()
            import time; time.sleep(0.3)

        wws.start_background(loop=kernel_loop, mode=target_mode)
        return jsonify({"status": "ok", "mode": target_mode, "state": getattr(wws, "state", "standby")})

    else:
        return jsonify({"status": "error", "message": f"Unknown action '{action}'"}), 400


# =====================================================================
# ALEXA SKILL WEBHOOK ENDPOINT
# =====================================================================
@app.route("/alexa", methods=["GET", "POST"])
def alexa_webhook():
    if request.method == "GET":
        return jsonify({
            "status": "ready",
            "message": "AIVA Alexa Webhook is active and waiting for Alexa POST requests."
        })

    data = request.json or {}
    req = data.get("request", {})
    req_type = req.get("type", "")

    print(f"\n[Alexa] Received Request Type: {req_type}")

    # 1. Launch Request ("Alexa, open AIVA")
    if req_type == "LaunchRequest":
        user_name = "Charan"
        try:
            user_name = kernel.user_memory_service.get("name") or "Charan"
        except Exception:
            pass

        greeting = f"AIVA is online and connected to your PC. How can I assist you today, {user_name}?"
        reprompt = "I'm listening. You can ask me to open apps, do research, or manage your memory."
        return jsonify(build_alexa_response(greeting, should_end_session=False, reprompt_text=reprompt))

    # 2. Intent Request
    elif req_type == "IntentRequest":
        intent = req.get("intent", {})
        intent_name = intent.get("name", "")
        print(f"[Alexa] Intent: {intent_name}")

        # Stop / Cancel Intents
        if intent_name in {"AMAZON.StopIntent", "AMAZON.CancelIntent"}:
            return jsonify(build_alexa_response("AIVA session ended. Goodbye!", should_end_session=True))

        # Help Intent
        if intent_name == "AMAZON.HelpIntent":
            help_text = (
                "I am AIVA, your autonomous PC assistant. You can ask me to open applications, "
                "search YouTube or Google, conduct deep web research, or take notes. What would you like me to do?"
            )
            return jsonify(build_alexa_response(help_text, should_end_session=False, reprompt_text="What would you like me to do?"))

        # Catch-All Prompt Intent or Fallback Intent
        query = ""
        slots = intent.get("slots", {})
        if "query" in slots and slots["query"].get("value"):
            query = slots["query"]["value"]
        elif intent_name == "AMAZON.FallbackIntent":
            query = req.get("dialogState", "") or ""

        if not query:
            return jsonify(build_alexa_response("I didn't catch that. Could you please say that again?", should_end_session=False))

        print(f"[Alexa Prompt]: '{query}'")

        try:
            # Process query through AIVA without PC TTS (since Echo will speak the response)
            result = run_async(kernel.input_handler.process_query(query, speak=False))
            response_text = result.get("response", "Command executed.")
            should_end = result.get("should_end_session", False)
            reprompt = "What else can I help you with?" if not should_end else None

            return jsonify(build_alexa_response(response_text, should_end_session=should_end, reprompt_text=reprompt))

        except Exception as e:
            print(f"[Alexa Error] Failed to process query: {e}")
            return jsonify(build_alexa_response("I ran into an issue executing that command on your PC. Please check the terminal logs.", should_end_session=False))

    # 3. Session Ended Request
    elif req_type == "SessionEndedRequest":
        print("[Alexa] Session ended by user or timeout.")
        return jsonify({"version": "1.0", "response": {}})

    return jsonify(build_alexa_response("I didn't understand that request.", should_end_session=False))


# =====================================================================
# SIMPLE REST WEBHOOK (For direct HTTP calls / IFTTT / Alexa Routines)
# =====================================================================
@app.route("/command", methods=["POST"])
def direct_command():
    data = request.json or {}
    msg = data.get("message") or data.get("query") or ""

    if not msg:
        return jsonify({"status": "error", "message": "Missing 'message' field"}), 400

    print(f"\n[HTTP Command]: {msg}")
    try:
        result = run_async(kernel.input_handler.process_query(msg, speak=True))
        return jsonify({
            "status": "success",
            "query": msg,
            "response": result.get("response", ""),
            "route": result.get("route", "")
        })
    except Exception as e:
        return jsonify({"status": "error", "error": str(e)}), 500


@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "healthy", "kernel_active": kernel is not None and kernel.running})


# =====================================================================
# SERVER LIFECYCLE & INITIALIZATION
# =====================================================================
def start_kernel_background():
    global kernel, kernel_loop
    kernel_loop = asyncio.new_event_loop()
    asyncio.set_event_loop(kernel_loop)
    kernel = AIVAKernel()
    kernel_loop.run_until_complete(kernel.start_server())
    # Start the adaptive voice runtime (listens on mic if available, harmlessly skips if headless)
    try:
        kernel.start_voice_mode(mode="adaptive")
    except Exception as e:
        print(f"[Warning] Headless server - voice mode not started: {e}")
    kernel_loop.run_forever()


def main():
    global kernel_thread
    print("=" * 50)
    print("🚀 AIVA ALEXA & WEBHOOK SERVER")
    print("=" * 50)
    print("[1/2] Initializing AIVA Runtime Kernel in background...")

    kernel_thread = threading.Thread(target=start_kernel_background, daemon=True)
    kernel_thread.start()

    # Wait briefly for kernel to initialize
    import time
    time.sleep(2)

    print("[2/2] Starting Flask HTTP Server on port 5000...")
    print("📌 Alexa Endpoint: http://localhost:5000/alexa")
    print("📌 REST Endpoint:  http://localhost:5000/command")
    print("📌 Health Check:   http://localhost:5000/health")
    print("=" * 50)

    app.run(host="0.0.0.0", port=5000, debug=False, use_reloader=False)


if __name__ == "__main__":
    main()