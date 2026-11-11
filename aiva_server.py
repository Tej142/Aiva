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
from functools import wraps
from flask import Flask, request, jsonify, send_from_directory, make_response, send_file, redirect
from flask_cors import CORS
from flask_sock import Sock

from core.memory import MemoryEngine
from core.runtime.kernel import AIVAKernel
from services.database_service import DatabaseService
from services.auth_service import AuthService
from services.device_manager import DeviceManager
from services.device_channel import DeviceChannel

UI_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ui")
app = Flask(__name__, static_folder=UI_DIR)
CORS(app, supports_credentials=True)
sock = Sock(app)

# =====================================================================
# DATABASE, AUTH & DEVICE SERVICES
# =====================================================================
db = DatabaseService()
auth_service = AuthService(db)
device_manager = DeviceManager(db)
device_channel = DeviceChannel(db, device_manager)

_fallback_memory_engine = None
_memory_engine_lock = threading.RLock()
_hud_ws_clients = {}
_hud_ws_lock = threading.RLock()


def _get_memory_engine() -> MemoryEngine:
    """Return active kernel's MemoryEngine or a lazily cached engine bound to current `db`."""
    global _fallback_memory_engine
    if kernel and getattr(kernel, "memory_engine", None) and kernel.memory_engine.db is db:
        return kernel.memory_engine
    with _memory_engine_lock:
        if _fallback_memory_engine is None or _fallback_memory_engine.db is not db:
            _fallback_memory_engine = MemoryEngine(db=db, start_worker=False)
        return _fallback_memory_engine


def _deliver_to_hud(user_id: str, payload: dict) -> bool:
    """Push proactive reminder / briefing notifications over open HUD WebSockets."""
    delivered = False
    msg_json = json.dumps({"type": "memory_notification", **payload})
    with _hud_ws_lock:
        conns = list(_hud_ws_clients.get(user_id, set()))
        dead = []
        for ws in conns:
            try:
                ws.send(msg_json)
                delivered = True
            except Exception:
                dead.append(ws)
        for ws in dead:
            _hud_ws_clients.get(user_id, set()).discard(ws)

    # Also notify any online Windows agent sockets owned by user_id
    if device_channel:
        with device_channel._channel_lock:
            for dev_id, ws in list(device_channel._connections.items()):
                if device_channel._device_user_map.get(dev_id) == user_id:
                    try:
                        ws.send(msg_json)
                        delivered = True
                    except Exception:
                        pass
    return delivered


def get_authenticated_user():
    """Extract and verify user from Bearer header, cookie, device token, or active channel."""
    auth_header = request.headers.get("Authorization", "")
    token = None
    if auth_header.startswith("Bearer "):
        token = auth_header[7:].strip()
    elif "aiva_access_token" in request.cookies:
        token = request.cookies.get("aiva_access_token")

    if token:
        payload = auth_service.verify_access_token(token)
        if payload:
            user_id = payload.get("sub")
            if user_id:
                user = db.get_user_by_id(user_id)
                if user:
                    return user

    # Fallback 1: Device token header
    device_token = request.headers.get("X-Device-Token") or request.args.get("device_token")
    if device_token:
        device = device_manager.get_device_by_token(device_token)
        if device and device.get("user_id"):
            user = db.get_user_by_id(device["user_id"])
            if user:
                return user

    # Fallback 2: Connected companion device channel session fallback
    if device_channel and hasattr(device_channel, '_device_user_map') and device_channel._device_user_map:
        online_user_id = next(iter(device_channel._device_user_map.values()), None)
        if online_user_id:
            user = db.get_user_by_id(online_user_id)
            if user:
                return user

    return None


def require_auth(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        user = get_authenticated_user()
        if not user:
            return jsonify({"status": "error", "message": "Authentication required. Please log in."}), 401
        return f(user, *args, **kwargs)
    return decorated

# =====================================================================
# GLOBAL KERNEL & ASYNC THREAD RUNNER
# =====================================================================
kernel = None
kernel_loop = None
kernel_thread = None


def _clean_for_speech(text: str) -> str:
    """Clean markdown, emoji, and formatting so speech delivery is natural and crisp."""
    from services.voice_service import sanitize_for_speech
    return sanitize_for_speech(text)


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
# WINDOWS COMPANION APP INSTALLER DOWNLOAD
# =====================================================================
@app.route("/download/windows-agent", methods=["GET"])
@app.route("/download/agent", methods=["GET"])
def download_windows_agent():
    """
    Serves the Windows Device Agent installer (.exe) directly from the server,
    or falls back to the latest GitHub Release asset.
    """
    base_dir = os.path.dirname(os.path.abspath(__file__))
    candidates = [
        os.path.join(base_dir, "storage", "downloads", "AIVA-Device-Agent-Setup-1.0.0.exe"),
        os.path.join(base_dir, "windows-agent", "dist", "AIVA Device Agent Setup 1.0.0.exe")
    ]
    for path in candidates:
        if os.path.exists(path):
            return send_file(
                path,
                as_attachment=True,
                download_name="AIVA-Device-Agent-Setup-1.0.0.exe",
                mimetype="application/vnd.microsoft.portable-executable"
            )

    # Fallback: Redirect to GitHub Releases download URL
    return redirect("https://github.com/Tej142/Aiva/releases/latest/download/AIVA-Device-Agent-Setup-1.0.0.exe")


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

    # Strict user identification: require authenticated session
    auth_user = get_authenticated_user()
    if not auth_user:
        return jsonify({"status": "error", "error": "Authentication required. Please log in."}), 401

    if kernel:
        kernel.set_active_user(auth_user["id"])

    uid_log = auth_user["id"]
    print(f"\n[Web UI Chat Query]: {msg} (User: {uid_log})")
    try:
        result = None
        # Fast direct execution for screen inspection queries
        if any(w in msg.lower() for w in ["screen", "screenshot", "what is on my screen", "inspect screen"]) and kernel and hasattr(kernel, "capabilities"):
            inspect_handler = kernel.capabilities.get("inspect_screen")
            if inspect_handler:
                try:
                    v_res = run_async(inspect_handler({"query": msg, "user_id": auth_user["id"]}), timeout=15.0)
                    if v_res and v_res.get("analysis"):
                        result = {"response": v_res["analysis"], "route": "vision"}
                except Exception as v_err:
                    print(f"[VisionDirect Error]: {v_err}")

        # Run through AIVA's intelligent pipeline (Intent Classifier -> Router -> MemoryEngine / DAG / Tool)
        if not result:
            result = run_async(
                kernel.input_handler.process_query(
                    msg,
                    speak=False,
                    user_id=auth_user["id"],
                    channel="hud",
                )
            )
        
        reply_text = result.get("response", "Done.")
        response_data = {
            "status": "success",
            "query": msg,
            "response": reply_text,
            "route": result.get("route", "general"),
            "should_end_session": result.get("should_end_session", False)
        }

        # Synthesize neural voice audio (Edge TTS) for client playback
        if reply_text and kernel and hasattr(kernel, 'voice_service') and kernel.voice_service:
            try:
                import base64
                clean_tts = _clean_for_speech(reply_text)
                if clean_tts:
                    from services.voice_service import _has_telugu_script, VOICE_MOHAN, VOICE_PRABHAT, RATE_PRABHAT, RATE_MOHAN, PITCH_PRABHAT, PITCH_MOHAN
                    voice = VOICE_MOHAN if _has_telugu_script(clean_tts) else VOICE_PRABHAT
                    rate = RATE_MOHAN if _has_telugu_script(clean_tts) else RATE_PRABHAT
                    pitch = PITCH_MOHAN if _has_telugu_script(clean_tts) else PITCH_PRABHAT
                    audio_bytes = run_async(kernel.voice_service._generate_edge_tts_bytes(
                        clean_tts, voice, rate, pitch
                    ), timeout=6.0)
                    if audio_bytes:
                        response_data["audio_base64"] = base64.b64encode(audio_bytes).decode("utf-8")
            except Exception as tts_err:
                print(f"[TTS Generate Warning]: {tts_err}")

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
    online_count = len(device_channel._connections) if device_channel else 0
    return jsonify({
        "status": "online",
        "kernel_running": kernel is not None and kernel.running,
        "devices_online": online_count,
        "key_count": 17,
        "pools": {
            "groq": 7,
            "gemini": 5,
            "mistral": 5
        },
        "voice_engine": "Microsoft Edge Neural (en-IN-PrabhatNeural)"
    })


# =====================================================================
# AUTHENTICATION REST APIS (Brevo Transactional Email OTP)
# =====================================================================
@app.route("/api/auth/register", methods=["POST"])
def auth_register():
    data = request.json or {}
    email = data.get("email", "").strip()
    name = data.get("name", "").strip()
    if not email:
        return jsonify({"status": "error", "message": "Email address is required."}), 400

    ip = request.headers.get("X-Forwarded-For", request.remote_addr)
    success, msg = auth_service.request_otp(email, ip_address=ip, name=name)
    if not success:
        return jsonify({"status": "error", "message": msg}), 400

    return jsonify({"status": "success", "message": msg})


@app.route("/api/auth/login", methods=["POST"])
def auth_login():
    data = request.json or {}
    email = data.get("email", "").strip()
    if not email:
        return jsonify({"status": "error", "message": "Email address is required."}), 400

    ip = request.headers.get("X-Forwarded-For", request.remote_addr)
    success, msg = auth_service.request_otp(email, ip_address=ip)
    if not success:
        return jsonify({"status": "error", "message": msg}), 400

    return jsonify({"status": "success", "message": msg})


@app.route("/api/auth/verify-otp", methods=["POST"])
def auth_verify_otp():
    data = request.json or {}
    email = data.get("email", "").strip()
    otp = str(data.get("otp", "")).strip()
    name = data.get("name", "").strip()

    if not email or not otp:
        return jsonify({"status": "error", "message": "Email and 6-digit OTP code are required."}), 400

    success, msg, result = auth_service.verify_otp(email, otp, name=name)
    if not success:
        return jsonify({"status": "error", "message": msg}), 400

    response = make_response(jsonify({
        "status": "success",
        "message": msg,
        "user": result["user"],
        "tokens": result["tokens"]
    }))

    # Set cookie for shared browser/Electron session
    response.set_cookie(
        "aiva_access_token",
        result["tokens"]["access_token"],
        max_age=result["tokens"]["expires_in"],
        httponly=False,
        samesite="Lax"
    )
    return response


@app.route("/api/auth/onboarding", methods=["POST"])
@require_auth
def auth_onboarding(user):
    data = request.json or {}
    name = data.get("name", "").strip()
    db.update_user(user["id"], name=name or None, onboarded=1)
    return jsonify({"status": "success", "message": "Onboarding completed successfully."})


@app.route("/api/auth/refresh", methods=["POST"])
def auth_refresh():
    data = request.json or {}
    refresh_token = data.get("refresh_token") or request.cookies.get("aiva_refresh_token")

    if not refresh_token:
        return jsonify({"status": "error", "message": "Refresh token is required."}), 400

    success, msg, tokens = auth_service.refresh_access_token(refresh_token)
    if not success:
        return jsonify({"status": "error", "message": msg}), 401

    response = make_response(jsonify({
        "status": "success",
        "message": msg,
        "tokens": tokens
    }))
    response.set_cookie(
        "aiva_access_token",
        tokens["access_token"],
        max_age=tokens["expires_in"],
        httponly=False,
        samesite="Lax"
    )
    return response


@app.route("/api/auth/logout", methods=["POST"])
def auth_logout():
    data = request.json or {}
    refresh_token = data.get("refresh_token")
    if refresh_token:
        auth_service.revoke_refresh_token(refresh_token)

    response = make_response(jsonify({"status": "success", "message": "Logged out successfully."}))
    response.delete_cookie("aiva_access_token")
    return response


@app.route("/api/auth/me", methods=["GET"])
@require_auth
def auth_me(user):
    devices = device_manager.list_devices(user["id"])
    with device_channel._channel_lock:
        online_ids = set(device_channel._connections.keys())

    for dev in devices:
        dev["is_online"] = dev["id"] in online_ids

    fresh_user = db.get_user_by_id(user["id"]) or user
    return jsonify({
        "status": "success",
        "user": {
            "id": fresh_user["id"],
            "email": fresh_user["email"],
            "name": fresh_user.get("name", ""),
            "onboarded": fresh_user.get("onboarded", 0),
            "created_at": fresh_user["created_at"]
        },
        "devices": devices
    })


# =====================================================================
# DEVICE REGISTRY REST APIS
# =====================================================================
@app.route("/api/devices", methods=["GET"])
@require_auth
def get_devices(user):
    devices = device_manager.list_devices(user["id"])
    with device_channel._channel_lock:
        online_ids = set(device_channel._connections.keys())

    for dev in devices:
        dev["is_online"] = dev["id"] in online_ids

    return jsonify({"status": "success", "devices": devices})


@app.route("/api/devices/register", methods=["POST"])
@require_auth
def register_device(user):
    data = request.json or {}
    name = data.get("name") or "Windows PC"
    platform_str = data.get("platform") or "windows"
    app_version = data.get("app_version") or "1.0.0"

    success, msg, result = device_manager.register_device(
        user_id=user["id"],
        name=name,
        platform=platform_str,
        app_version=app_version
    )
    if not success:
        return jsonify({"status": "error", "message": msg}), 400

    return jsonify({
        "status": "success",
        "message": msg,
        "device": result["device"],
        "device_token": result["device_token"]
    }), 201


@app.route("/api/devices/<device_id>", methods=["PATCH"])
@require_auth
def rename_device(user, device_id):
    data = request.json or {}
    new_name = data.get("name", "").strip()
    if not new_name:
        return jsonify({"status": "error", "message": "Device name is required."}), 400

    success, msg = device_manager.rename_device(device_id=device_id, user_id=user["id"], new_name=new_name)
    if not success:
        return jsonify({"status": "error", "message": msg}), 404

    return jsonify({"status": "success", "message": msg})


@app.route("/api/devices/<device_id>", methods=["DELETE"])
@require_auth
def revoke_device(user, device_id):
    # Terminate active socket connection if open
    with device_channel._channel_lock:
        ws = device_channel._connections.get(device_id)
        if ws:
            try:
                ws.send(json.dumps({
                    "v": 1,
                    "type": "revoked",
                    "message": "Device token has been revoked by user."
                }))
                ws.close(reason="Device revoked")
            except Exception:
                pass
            device_channel._connections.pop(device_id, None)
            device_channel._device_user_map.pop(device_id, None)

    success, msg = device_manager.revoke_device(device_id=device_id, user_id=user["id"])
    if not success:
        return jsonify({"status": "error", "message": msg}), 404

    return jsonify({"status": "success", "message": msg})


@app.route("/api/devices/audit-logs", methods=["GET"])
@require_auth
def get_audit_logs(user):
    limit = request.args.get("limit", 50, type=int)
    logs = db.get_recent_audit_logs(user["id"], limit=limit)
    return jsonify({"status": "success", "audit_logs": logs})


# =====================================================================
# REAL-TIME SECURE WEBSOCKET ENDPOINTS (/ws/device & /ws/hud)
# =====================================================================
@sock.route("/ws/device")
def ws_device_endpoint(ws):
    device_channel.handle_ws_connection(ws, request.environ)


@sock.route("/ws/hud")
def ws_hud_endpoint(ws):
    """Authenticated WebSocket channel for HUD notifications, proactive reminders & briefings."""
    token = request.args.get("token") or request.cookies.get("aiva_access_token")
    if not token:
        auth_header = request.headers.get("Authorization", "")
        if auth_header.startswith("Bearer "):
            token = auth_header[7:].strip()
    payload = auth_service.verify_access_token(token) if token else None
    if not payload:
        try:
            ws.send(json.dumps({"type": "error", "error": "unauthorized"}))
        except Exception:
            pass
        return
    uid = payload.get("sub") or payload.get("user_id")
    if not uid:
        return
    with _hud_ws_lock:
        _hud_ws_clients.setdefault(uid, set()).add(ws)
    try:
        ws.send(json.dumps({"type": "connected", "channel": "hud"}))
        # Flush recent undelivered notifications on connect
        mem = _get_memory_engine()
        recent = mem.scheduler.recent_notifications.get(uid, [])[-5:]
        for item in recent:
            ws.send(json.dumps({"type": "memory_notification", **item}))
        while True:
            msg = ws.receive(timeout=30)
            if msg is None:
                # Poll timeout or closed; verify connection on next cycle
                continue
    except Exception:
        pass
    finally:
        with _hud_ws_lock:
            _hud_ws_clients.get(uid, set()).discard(ws)


# =====================================================================
# UNIFIED MEMORY & REMINDER APIS (JWT-protected, user_id-scoped)
# =====================================================================
@app.route("/api/memory", methods=["GET"])
@require_auth
def api_memory(user):
    """Fetch memories (searchable, filterable by type), entities, reminders, settings, and task patterns."""
    uid = user["id"]
    engine = _get_memory_engine()
    mem_type = request.args.get("type") or None
    search_q = (request.args.get("q") or request.args.get("search") or "").strip() or None

    items = engine.list_memories(uid, mem_type=mem_type, search=search_q, limit=250)
    grouped = {}
    for m in items:
        grouped.setdefault(m["type"], []).append(m)

    entities = engine.list_entities(uid)
    reminders = engine.list_reminders(uid)
    settings = engine.get_settings(uid)
    task_patterns = db.get_user_task_patterns(uid)
    return jsonify({
        "status": "success",
        "memories": items,
        "grouped": grouped,
        "entities": entities,
        "reminders": reminders,
        "settings": settings,
        "preferred_name": engine.get_preferred_name(uid),
        "display_name": engine.get_user_display_name(uid),
        "task_patterns": task_patterns,
    })


@app.route("/api/memory/<memory_id>", methods=["PATCH", "DELETE"])
@require_auth
def api_memory_item(user, memory_id):
    """Edit or permanently forget a single memory belonging to the authenticated user."""
    uid = user["id"]
    engine = _get_memory_engine()
    if request.method == "DELETE":
        ok = engine.forget_memory(uid, memory_id)
        if not ok:
            return jsonify({"status": "error", "error": "Memory not found"}), 404
        return jsonify({"status": "success", "deleted": memory_id})

    body = request.get_json(silent=True) or {}
    try:
        updated = engine.update_memory(
            uid,
            memory_id,
            content=body.get("content"),
            summary=body.get("summary"),
            importance=body.get("importance"),
            mem_type=body.get("type"),
            entities=body.get("entities"),
        )
    except ValueError as err:
        return jsonify({"status": "error", "error": str(err)}), 400
    if not updated:
        return jsonify({"status": "error", "error": "Memory not found"}), 404
    return jsonify({"status": "success", "memory": updated})


@app.route("/api/memory/forget-all", methods=["POST"])
@require_auth
def api_memory_forget_all(user):
    """Erase all memories, entities, links, FTS entries, embeddings, and reminders for one user."""
    uid = user["id"]
    body = request.get_json(silent=True) or {}
    if body.get("confirm") is not True:
        return jsonify({
            "status": "error",
            "error": "confirmation_required",
            "message": "Pass {\"confirm\": true} to permanently erase all your memory data.",
        }), 400
    deleted = _get_memory_engine().forget_all(uid)
    return jsonify({"status": "success", "deleted": deleted})


@app.route("/api/memory/export", methods=["GET"])
@require_auth
def api_memory_export(user):
    """Download complete user memory export as JSON."""
    uid = user["id"]
    export_payload = _get_memory_engine().export_user_data(uid)
    resp = make_response(json.dumps(export_payload, indent=2))
    resp.headers["Content-Type"] = "application/json"
    resp.headers["Content-Disposition"] = 'attachment; filename="aiva-memory-export.json"'
    return resp


@app.route("/api/memory/settings", methods=["GET", "PATCH"])
@require_auth
def api_memory_settings(user):
    """Read or update per-user memory controls (memory_enabled, learning_paused, quiet_hours, morning_briefing)."""
    uid = user["id"]
    engine = _get_memory_engine()
    if request.method == "PATCH":
        patch = request.get_json(silent=True) or {}
        settings = engine.update_settings(uid, patch)
        return jsonify({"status": "success", "settings": settings})
    return jsonify({"status": "success", "settings": engine.get_settings(uid)})


@app.route("/api/reminders", methods=["GET", "POST"])
@require_auth
def api_reminders(user):
    """List or create prospective reminders scoped to authenticated user."""
    uid = user["id"]
    engine = _get_memory_engine()
    if request.method == "GET":
        status_filter = request.args.get("status") or None
        reminders = engine.list_reminders(uid, status=status_filter)
        return jsonify({"status": "success", "reminders": reminders})

    body = request.get_json(silent=True) or {}
    text = (body.get("text") or "").strip()
    due_at = body.get("due_at")
    if not text or due_at is None:
        return jsonify({"status": "error", "error": "text and due_at are required"}), 400
    res = engine.create_reminder(
        user_id=uid,
        text=text,
        due_at=due_at,
        recurrence=body.get("recurrence"),
        channel=body.get("channel"),
        urgent=bool(body.get("urgent", False)),
    )
    if res.get("status") != "success":
        return jsonify(res), 400
    return jsonify(res), 201


@app.route("/api/reminders/<reminder_id>", methods=["PATCH", "DELETE"])
@require_auth
def api_reminder_item(user, reminder_id):
    """Update status/snooze or cancel a reminder belonging to the authenticated user."""
    uid = user["id"]
    engine = _get_memory_engine()
    if request.method == "DELETE":
        updated = engine.store.update_reminder(uid, reminder_id, status="cancelled")
        if not updated:
            return jsonify({"status": "error", "error": "Reminder not found"}), 404
        return jsonify({"status": "success", "reminder": updated.to_dict()})

    body = request.get_json(silent=True) or {}
    if body.get("snooze_seconds"):
        rem = engine.scheduler.snooze_reminder(uid, reminder_id=reminder_id, duration_seconds=int(body["snooze_seconds"]))
        if not rem:
            return jsonify({"status": "error", "error": "Reminder not found"}), 404
        return jsonify({"status": "success", "reminder": rem.to_dict()})

    updated = engine.store.update_reminder(
        uid,
        reminder_id,
        status=body.get("status"),
        due_at=body.get("due_at"),
        text=body.get("text"),
    )
    if not updated:
        return jsonify({"status": "error", "error": "Reminder not found"}), 404
    return jsonify({"status": "success", "reminder": updated.to_dict()})


@app.route("/api/history", methods=["GET", "DELETE"])
@require_auth
def api_history(user):
    """Fetches or clears conversation history from MemoryEngine scoped strictly to the authenticated user."""
    uid = user["id"]
    engine = _get_memory_engine()
    if request.method == "DELETE":
        engine.clear_history(uid)
        return jsonify({"status": "success", "message": "Conversation history cleared."})
    history = engine.get_history(uid, limit=100)
    return jsonify({"status": "success", "history": history})


@app.route("/api/settings", methods=["GET", "POST"])
@require_auth
def api_user_settings(user):
    """Fetches or saves personal user settings/preferences."""
    uid = user["id"]
    if request.method == "POST":
        settings_data = request.json or {}
        existing = db.get_user_settings(uid) or {}
        existing.update(settings_data)
        db.set_user_settings(uid, existing)
        return jsonify({"status": "success", "settings": existing})
    return jsonify({"status": "success", "settings": db.get_user_settings(uid)})


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

@app.route("/api/voice/transcribe", methods=["POST"])
def api_voice_transcribe():
    """
    Transcribes audio from the Web HUD / Electron desktop app using Groq whisper-large-v3.
    """
    auth_user = get_authenticated_user()
    if not auth_user:
        return jsonify({"status": "error", "error": "Authentication required"}), 401

    audio_bytes = None
    filename = "speech.webm"

    if "audio" in request.files:
        f = request.files["audio"]
        audio_bytes = f.read()
        filename = f.filename or "speech.webm"
    elif request.data:
        audio_bytes = request.data
        ctype = request.content_type or ""
        if "wav" in ctype:
            filename = "speech.wav"
        elif "mp4" in ctype:
            filename = "speech.mp4"
        elif "ogg" in ctype:
            filename = "speech.ogg"

    if not audio_bytes:
        return jsonify({"status": "error", "error": "No audio data provided"}), 400

    try:
        import time
        t0 = time.perf_counter()
        from core.shared.config_loader import load_secrets
        import groq

        secrets = load_secrets()
        keys = secrets.get("groq_api_keys", [])
        if not keys and secrets.get("groq_api_key"):
            keys = [secrets.get("groq_api_key")]

        if not keys:
            return jsonify({"status": "error", "error": "No Groq API keys configured for transcription"}), 500

        transcript_text = ""
        last_err = None
        for key in keys:
            try:
                client = groq.Groq(api_key=key)
                transcription = client.audio.transcriptions.create(
                    file=(filename, audio_bytes),
                    model="whisper-large-v3"
                )
                transcript_text = getattr(transcription, 'text', str(transcription)).strip()
                duration_ms = (time.perf_counter() - t0) * 1000
                print(f"[VoicePipeline] stage=stt bytes={len(audio_bytes)} duration_ms={duration_ms:.1f} status=ok")
                if transcript_text:
                    break
            except Exception as e:
                last_err = e
                continue

        if not transcript_text and last_err:
            raise last_err

        return jsonify({"status": "success", "text": transcript_text})
    except Exception as e:
        print(f"[Voice Transcribe Error]: {e}")
        return jsonify({"status": "error", "error": str(e)}), 500


@app.route("/api/voice/tts", methods=["GET", "POST"])
def api_voice_tts():
    """
    Streams Microsoft Edge Neural voice MP3 audio for given text.
    """
    text_input = ""
    if request.method == "POST":
        data = request.json or {}
        text_input = data.get("text", "")
    else:
        text_input = request.args.get("text", "")

    if not text_input:
        return jsonify({"status": "error", "error": "Missing text"}), 400

    clean_text = _clean_for_speech(text_input)
    if not clean_text:
        return jsonify({"status": "error", "error": "No speakable text"}), 400

    try:
        import io
        from services.voice_service import _has_telugu_script, VOICE_MOHAN, VOICE_PRABHAT, RATE_PRABHAT, RATE_MOHAN, PITCH_PRABHAT, PITCH_MOHAN

        voice = VOICE_MOHAN if _has_telugu_script(clean_text) else VOICE_PRABHAT
        rate = RATE_MOHAN if _has_telugu_script(clean_text) else RATE_PRABHAT
        pitch = PITCH_MOHAN if _has_telugu_script(clean_text) else PITCH_PRABHAT

        audio_bytes = run_async(kernel.voice_service._generate_edge_tts_bytes(
            clean_text, voice, rate, pitch
        ), timeout=8.0)

        if not audio_bytes:
            return jsonify({"status": "error", "error": "Failed to synthesize audio"}), 500

        return send_file(
            io.BytesIO(audio_bytes),
            mimetype="audio/mpeg",
            as_attachment=False,
            download_name="speech.mp3"
        )
    except Exception as e:
        print(f"[TTS Stream Error]: {e}")
        return jsonify({"status": "error", "error": str(e)}), 500



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
            uid = getattr(kernel, "active_user_id", None)
            user_name = kernel.memory_engine.get_user_display_name(uid) or "Charan"
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
            # Bind user context to kernel if available
            if kernel and not kernel.active_user_id:
                primary = db.get_primary_user()
                if primary:
                    kernel.active_user_id = primary["id"]

            # Process query through AIVA without PC TTS (since Echo will speak the response)
            result = run_async(
                kernel.input_handler.process_query(
                    query,
                    speak=False,
                    user_id=getattr(kernel, "active_user_id", None),
                    channel="voice",
                )
            )
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

    auth_user = get_authenticated_user()
    if auth_user and kernel:
        kernel.active_user_id = auth_user["id"]
    elif kernel and not kernel.active_user_id:
        primary = db.get_primary_user()
        if primary:
            kernel.active_user_id = primary["id"]

    print(f"\n[HTTP Command]: {msg} (User: {getattr(kernel, 'active_user_id', 'none')})")
    try:
        result = run_async(
            kernel.input_handler.process_query(
                msg,
                speak=True,
                user_id=getattr(kernel, "active_user_id", None),
                channel="hud",
            )
        )
        return jsonify({
            "status": "success",
            "query": msg,
            "response": result.get("response", ""),
            "route": result.get("route", "")
        })
    except Exception as e:
        return jsonify({"status": "error", "error": str(e)}), 500


# =====================================================================
# WHATSAPP CHANNEL ENDPOINTS
# =====================================================================
@app.route("/internal/whatsapp/inbound", methods=["POST"])
def internal_whatsapp_inbound():
    """
    Inbound webhook exclusively for the local Baileys Node bridge.
    Rejects non-localhost callers and validates X-Bridge-Secret.
    """
    client_ip = request.remote_addr or ""
    if client_ip not in ("127.0.0.1", "::1", "localhost"):
        return jsonify({"error": "forbidden", "message": "Localhost only"}), 403

    from services.whatsapp_service import verify_bridge_secret
    secret = request.headers.get("X-Bridge-Secret", "")
    if not verify_bridge_secret(secret):
        return jsonify({"error": "unauthorized", "message": "Invalid bridge secret"}), 401

    if not kernel or not hasattr(kernel, "whatsapp_service"):
        return jsonify({"error": "service_unavailable", "message": "Kernel WhatsApp service not ready"}), 503

    payload = request.get_json(silent=True) or {}
    
    if kernel_loop and kernel_loop.is_running():
        asyncio.run_coroutine_threadsafe(kernel.whatsapp_service.handle_inbound(payload), kernel_loop)
    else:
        run_async(kernel.whatsapp_service.handle_inbound(payload))

    return jsonify({"status": "received"}), 200


@app.route("/api/whatsapp/status", methods=["GET"])
@require_auth
def api_whatsapp_status(user):
    """Returns WhatsApp connection, pairing, and link status for authenticated user."""
    if not kernel or not hasattr(kernel, "whatsapp_service"):
        return jsonify({"status": "error", "message": "Kernel not ready"}), 503
    status = run_async(kernel.whatsapp_service.get_user_status(user["id"], user=user))
    return jsonify({"status": "success", "data": status})


@app.route("/api/whatsapp/pair/start", methods=["POST"])
@require_auth
def api_whatsapp_pair_start(user):
    """Starts Baileys WhatsApp device pairing mode (QR or 8-digit phone code)."""
    if not kernel or not hasattr(kernel, "whatsapp_service"):
        return jsonify({"status": "error", "message": "Kernel not ready"}), 503
    data = request.json or {}
    mode = data.get("mode", "qr")
    phone = data.get("phone", "")
    code, body = run_async(
        kernel.whatsapp_service.start_bridge_pairing(user, mode=mode, raw_phone=phone),
        timeout=15.0,
    )
    return jsonify(body), code


@app.route("/api/whatsapp/pair/cancel", methods=["POST"])
@require_auth
def api_whatsapp_pair_cancel(user):
    """Cancels an in-progress WhatsApp bridge pairing session."""
    if not kernel or not hasattr(kernel, "whatsapp_service"):
        return jsonify({"status": "error", "message": "Kernel not ready"}), 503
    code, body = run_async(kernel.whatsapp_service.cancel_bridge_pairing(user), timeout=10.0)
    return jsonify(body), code


@app.route("/api/whatsapp/pair/logout", methods=["POST"])
@require_auth
def api_whatsapp_pair_logout(user):
    """Logs out the active Baileys session and clears credentials for re-pairing."""
    if not kernel or not hasattr(kernel, "whatsapp_service"):
        return jsonify({"status": "error", "message": "Kernel not ready"}), 503
    code, body = run_async(kernel.whatsapp_service.logout_bridge(user), timeout=10.0)
    return jsonify(body), code


@app.route("/api/whatsapp/link/start", methods=["POST"])
@require_auth
def api_whatsapp_link_start(user):
    """Generates a 6-digit link OTP code for linking a WhatsApp number."""
    if not kernel or not hasattr(kernel, "whatsapp_service"):
        return jsonify({"status": "error", "message": "Kernel not ready"}), 503
    data = request.json or {}
    phone = data.get("phone", "")
    try:
        res = kernel.whatsapp_service.start_link(user["id"], phone)
        return jsonify({"status": "success", "data": res})
    except ValueError as e:
        return jsonify({"status": "error", "message": str(e)}), 400


@app.route("/api/whatsapp/link/unlink", methods=["POST"])
@require_auth
def api_whatsapp_link_unlink(user):
    """Unlinks a phone number from the user account."""
    if not kernel or not hasattr(kernel, "whatsapp_service"):
        return jsonify({"status": "error", "message": "Kernel not ready"}), 503
    data = request.json or {}
    phone = data.get("phone", "")
    unlinked = kernel.whatsapp_service.unlink_number(user["id"], phone)
    return jsonify({"status": "success", "unlinked": unlinked})


@app.route("/api/whatsapp/contacts", methods=["GET", "POST"])
@require_auth
def api_whatsapp_contacts(user):
    """Lists or adds WhatsApp contacts scoped to authenticated user."""
    uid = user["id"]
    if request.method == "POST":
        data = request.json or {}
        display_name = data.get("display_name", "").strip()
        phone = data.get("phone", "").strip()
        aliases = data.get("aliases", [])
        trusted = bool(data.get("trusted", True))
        is_self = bool(data.get("is_self", False))

        if not display_name or not phone:
            return jsonify({"status": "error", "message": "Display name and phone are required"}), 400

        contact = db.add_whatsapp_contact(
            user_id=uid,
            display_name=display_name,
            phone_e164=phone,
            aliases=aliases,
            is_self=is_self,
            trusted=trusted
        )
        return jsonify({"status": "success", "contact": contact})

    contacts = db.get_whatsapp_contacts(uid)
    return jsonify({"status": "success", "contacts": contacts})


@app.route("/api/whatsapp/contacts/<contact_id>", methods=["PUT", "DELETE"])
@require_auth
def api_whatsapp_contact_detail(user, contact_id):
    """Updates or deletes a specific WhatsApp contact scoped to user."""
    uid = user["id"]
    if request.method == "DELETE":
        deleted = db.delete_whatsapp_contact(uid, contact_id)
        return jsonify({"status": "success", "deleted": deleted})

    data = request.json or {}
    updated = db.update_whatsapp_contact(
        user_id=uid,
        contact_id=contact_id,
        display_name=data.get("display_name"),
        phone_e164=data.get("phone"),
        aliases=data.get("aliases"),
        trusted=data.get("trusted"),
        is_self=data.get("is_self")
    )
    if not updated:
        return jsonify({"status": "error", "message": "Contact not found"}), 404
    return jsonify({"status": "success", "contact": updated})


@app.route("/health", methods=["GET"])
def health():
    return jsonify({
        "status": "healthy",
        "kernel_active": kernel is not None and kernel.running,
        "devices_online": len(device_channel._connections) if device_channel else 0
    })


# =====================================================================
# SERVER LIFECYCLE & INITIALIZATION
# =====================================================================
def start_kernel_background():
    global kernel, kernel_loop
    kernel_loop = asyncio.new_event_loop()
    asyncio.set_event_loop(kernel_loop)
    kernel = AIVAKernel(db=db, device_manager=device_manager, device_channel=device_channel)
    kernel.device_channel = device_channel
    kernel.memory_engine.scheduler.hud_deliver = _deliver_to_hud
    device_channel.set_event_loop(kernel_loop)

    # Attach primary user if available
    primary = db.get_primary_user()
    if primary:
        kernel.active_user_id = primary["id"]

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