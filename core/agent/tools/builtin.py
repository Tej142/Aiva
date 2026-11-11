"""Built-in AIVA tool definitions wrapping existing capabilities, memory, research, and WhatsApp."""

from typing import Any, Dict, Optional

from core.agent.policy import RISK_CONFIRM, RISK_LOW, RISK_READ
from core.agent.tools.registry import ToolContext, ToolRegistry, ToolSpec


def _require_user(ctx: ToolContext) -> Optional[Dict[str, Any]]:
    if not ctx.user_id:
        return {"status": "error", "error": "unauthenticated", "message": "User is not authenticated."}
    return None


def _resolve_whatsapp_recipient(
    whatsapp_service: Any,
    db: Any,
    user_id: str,
    query: str,
) -> Dict[str, Any]:
    """Resolve contact by name/alias/phone with smart `is_self` fallback for 'second number'."""
    if not user_id:
        return {"status": "error", "error": "unauthenticated", "message": "User is not authenticated."}
    q = (query or "").strip()
    if not q:
        return {"status": "not_found", "message": "Please specify which contact to message."}

    if whatsapp_service and hasattr(whatsapp_service, "resolve_contact"):
        res = whatsapp_service.resolve_contact(user_id, q)
        if res.get("status") != "not_found":
            return res

    if db and hasattr(db, "resolve_whatsapp_contact"):
        matches = db.resolve_whatsapp_contact(user_id, q)
        if len(matches) == 1:
            return {"status": "found", "contact": matches[0]}
        if len(matches) > 1:
            names = [f"'{m['display_name']}'" for m in matches]
            return {
                "status": "ambiguous",
                "matches": matches,
                "message": f"Found multiple contacts matching '{q}': {', '.join(names)}. Which one did you mean?",
            }

    # Handle explicit references to user's secondary/self number
    low = q.lower()
    if any(tok in low for tok in ("second number", "2nd number", "my other number", "myself", "self")):
        contacts = db.get_whatsapp_contacts(user_id) if db and hasattr(db, "get_whatsapp_contacts") else []
        self_contacts = [c for c in contacts if c.get("is_self")]
        if len(self_contacts) == 1:
            return {"status": "found", "contact": self_contacts[0]}
        if len(self_contacts) > 1:
            return {
                "status": "ambiguous",
                "matches": self_contacts,
                "message": "Found multiple secondary numbers saved. Which one should I use?",
            }

    return {
        "status": "not_found",
        "message": f"I couldn't find '{q}' in your WhatsApp contacts. Please save them first.",
    }


def register_builtin_tools(
    registry: ToolRegistry,
    *,
    kernel: Any = None,
    capability_registry: Any = None,
    research_agent: Any = None,
    whatsapp_service: Any = None,
    memory_engine: Any = None,
    db: Any = None,
) -> ToolRegistry:
    """Register all Phase 1 built-in tools onto `registry`."""

    async def _call_cap(cap_name: str, ctx: ToolContext, params: Dict[str, Any]) -> Dict[str, Any]:
        cap = capability_registry.get(cap_name) if capability_registry else None
        if cap is None:
            return {"status": "error", "error": "capability_unavailable", "message": f"Capability '{cap_name}' is unavailable."}
        action_payload = {
            "action": cap_name,
            "user_id": ctx.user_id,
            "channel": ctx.channel,
            "device_name": ctx.device_name,
            "parameters": dict(params),
            **params,
        }
        res = await cap(action_payload)
        if not isinstance(res, dict):
            return {"status": "success", "result": res}
        return res

    # 1. open_apps_urls
    async def handle_open_apps_urls(args: Dict[str, Any], ctx: ToolContext) -> Dict[str, Any]:
        if args.get("url"):
            return await _call_cap("open_url", ctx, {"url": args["url"]})
        if args.get("app_name"):
            return await _call_cap("launch_app", ctx, {"app_name": args["app_name"]})
        return {"status": "error", "error": "missing_params", "message": "Provide either 'app_name' or 'url'."}

    registry.register(ToolSpec(
        name="open_apps_urls",
        description="Open a desktop application on the user's PC or open a website URL in the browser.",
        parameters={
            "type": "object",
            "properties": {
                "app_name": {"type": "string", "description": "Application name, e.g. 'chrome', 'notepad', 'spotify', 'vscode'."},
                "url": {"type": "string", "description": "Full URL to open, e.g. 'https://youtube.com'."},
            },
        },
        handler=handle_open_apps_urls,
        risk=RISK_LOW,
        allowed_channels=("hud", "voice", "whatsapp"),
        timeout_s=12.0,
        category="action",
    ))

    # 2. media_volume
    async def handle_media_volume(args: Dict[str, Any], ctx: ToolContext) -> Dict[str, Any]:
        kind = args.get("kind", "volume")
        if kind == "media":
            return await _call_cap("media_control", ctx, {"media_action": args.get("media_action", "play_pause")})
        params: Dict[str, Any] = {}
        if args.get("mute"):
            params["action"] = "mute"
        elif args.get("level") is not None:
            params["level"] = int(args["level"])
        else:
            params["delta"] = int(args.get("delta", 10))
        return await _call_cap("adjust_volume", ctx, params)

    registry.register(ToolSpec(
        name="media_volume",
        description="Control system audio volume (level 0-100, delta, or mute) or media playback (play_pause, next, previous, stop).",
        parameters={
            "type": "object",
            "properties": {
                "kind": {"type": "string", "enum": ["volume", "media"]},
                "level": {"type": "integer", "minimum": 0, "maximum": 100},
                "delta": {"type": "integer", "minimum": -100, "maximum": 100},
                "mute": {"type": "boolean"},
                "media_action": {"type": "string", "enum": ["play_pause", "next", "previous", "stop"]},
            },
            "required": ["kind"],
        },
        handler=handle_media_volume,
        risk=RISK_LOW,
        allowed_channels=("hud", "voice", "whatsapp"),
        timeout_s=10.0,
        category="action",
    ))

    # 3. keyboard_input
    async def handle_keyboard_input(args: Dict[str, Any], ctx: ToolContext) -> Dict[str, Any]:
        op = args["operation"]
        if op == "type_text":
            return await _call_cap("type_text", ctx, {"text": args.get("text", ""), "instant_paste": bool(args.get("instant_paste", True))})
        if op == "press_key":
            return await _call_cap("press_key", ctx, {"key": args.get("key", "enter"), "count": int(args.get("count", 1))})
        if op == "hotkey":
            return await _call_cap("hotkey", ctx, {"keys": args.get("keys") or []})
        return {"status": "error", "error": "invalid_operation"}

    registry.register(ToolSpec(
        name="keyboard_input",
        description="Send keyboard input on the active desktop window: type text, press a key, or trigger a hotkey.",
        parameters={
            "type": "object",
            "properties": {
                "operation": {"type": "string", "enum": ["type_text", "press_key", "hotkey"]},
                "text": {"type": "string"},
                "key": {"type": "string"},
                "count": {"type": "integer", "minimum": 1, "maximum": 20},
                "keys": {"type": "array", "items": {"type": "string"}},
                "instant_paste": {"type": "boolean"},
            },
            "required": ["operation"],
        },
        handler=handle_keyboard_input,
        risk=RISK_LOW,
        allowed_channels=("hud", "voice"),
        timeout_s=12.0,
        parallel_safe=False,
        category="action",
    ))

    # 4. screenshot
    async def handle_screenshot(args: Dict[str, Any], ctx: ToolContext) -> Dict[str, Any]:
        mode = args.get("mode", "capture")
        if mode == "inspect":
            return await _call_cap("inspect_screen", ctx, {"query": args.get("query") or "Describe what is visible on screen."})
        return await _call_cap("take_screenshot", ctx, {})

    registry.register(ToolSpec(
        name="screenshot",
        description="Capture a screenshot of the user's Windows screen or visually inspect what is on screen.",
        parameters={
            "type": "object",
            "properties": {
                "mode": {"type": "string", "enum": ["capture", "inspect"]},
                "query": {"type": "string", "description": "Question to answer about the screen when mode='inspect'."},
            },
        },
        handler=handle_screenshot,
        risk=RISK_READ,
        allowed_channels=("hud", "voice", "whatsapp"),
        timeout_s=20.0,
        category="action",
    ))

    # 5. power_control
    async def handle_power_control(args: Dict[str, Any], ctx: ToolContext) -> Dict[str, Any]:
        return await _call_cap("system_power", ctx, {"power_action": args["action"]})

    registry.register(ToolSpec(
        name="power_control",
        description="Control PC power state: lock screen, sleep, restart, or shutdown (destructive actions require confirmation).",
        parameters={
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["lock", "sleep", "restart", "shutdown"]},
            },
            "required": ["action"],
        },
        handler=handle_power_control,
        risk=RISK_CONFIRM,
        allowed_channels=("hud", "voice"),  # Blocked on WhatsApp per security policy
        timeout_s=12.0,
        parallel_safe=False,
        category="action",
    ))

    # 6. file_access
    async def handle_file_access(args: Dict[str, Any], ctx: ToolContext) -> Dict[str, Any]:
        op = args["action"]
        if op == "create_doc":
            return await _call_cap("create_document", ctx, {
                "file_name": args.get("file_name") or "notes.docx",
                "topic": args.get("topic") or "",
                "content": args.get("content") or "",
            })
        if op == "list_apps":
            return await _call_cap("list_running_apps", ctx, {})
        if op == "close_app":
            return await _call_cap("close_app", ctx, {"app_name": args.get("app_name") or ""})
        return await _call_cap("open_file", ctx, {"path": args.get("path") or args.get("file_name") or ""})

    registry.register(ToolSpec(
        name="file_access",
        description="Open a file, create a Word document, list running apps, or close a running application.",
        parameters={
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["open", "create_doc", "list_apps", "close_app"]},
                "path": {"type": "string"},
                "file_name": {"type": "string"},
                "topic": {"type": "string"},
                "content": {"type": "string"},
                "app_name": {"type": "string"},
            },
            "required": ["action"],
        },
        handler=handle_file_access,
        risk=RISK_LOW,
        allowed_channels=("hud", "voice"),
        timeout_s=15.0,
        parallel_safe=False,
        category="action",
    ))

    # 7. web_research
    async def handle_web_research(args: Dict[str, Any], ctx: ToolContext) -> Dict[str, Any]:
        topic = args["query"].strip()
        force_refresh = bool(args.get("force_refresh", False))
        if research_agent is not None:
            res = await research_agent.perform_research(topic, user_id=ctx.user_id, force_refresh=force_refresh)
            return res or {"status": "not_found", "message": f"No web results found for '{topic}'."}
        return await _call_cap("research", ctx, {"topic": topic, "force_refresh": force_refresh})

    registry.register(ToolSpec(
        name="web_research",
        description="Search the web for up-to-date facts, news, prices, schedules, or explanations and return a summary with sources.",
        parameters={
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Search topic or question."},
                "force_refresh": {"type": "boolean", "description": "Bypass recent knowledge cache if fresh search is requested."},
            },
            "required": ["query"],
        },
        handler=handle_web_research,
        risk=RISK_READ,
        allowed_channels=("hud", "voice", "whatsapp"),
        timeout_s=30.0,
        untrusted_result=True,
        parallel_safe=True,
        category="research",
    ))

    # 8. browser_actions
    async def handle_browser_actions(args: Dict[str, Any], ctx: ToolContext) -> Dict[str, Any]:
        return await _call_cap("web_automation", ctx, {"instruction": args["instruction"], "url": args.get("url", "")})

    registry.register(ToolSpec(
        name="browser_actions",
        description="Perform multi-step browser interaction or page inspection via Chrome automation.",
        parameters={
            "type": "object",
            "properties": {
                "instruction": {"type": "string", "description": "Natural language instruction for the browser task."},
                "url": {"type": "string"},
            },
            "required": ["instruction"],
        },
        handler=handle_browser_actions,
        risk=RISK_LOW,
        allowed_channels=("hud", "voice"),
        timeout_s=35.0,
        untrusted_result=True,
        parallel_safe=False,
        category="action",
    ))

    # 9. whatsapp_lookup_contact
    async def handle_wa_lookup(args: Dict[str, Any], ctx: ToolContext) -> Dict[str, Any]:
        err = _require_user(ctx)
        if err:
            return err
        return _resolve_whatsapp_recipient(whatsapp_service, db, ctx.user_id, args["query"])

    registry.register(ToolSpec(
        name="whatsapp_lookup_contact",
        description="Look up a saved WhatsApp contact by name, alias (e.g. 'dad'), phone suffix, or 'second number'.",
        parameters={
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Contact name, alias, phone, or 'second number'."},
            },
            "required": ["query"],
        },
        handler=handle_wa_lookup,
        risk=RISK_READ,
        allowed_channels=("hud", "voice", "whatsapp"),
        timeout_s=6.0,
        category="action",
    ))

    # 10. whatsapp_send
    def _preflight_wa_send(args: Dict[str, Any], ctx: ToolContext) -> Optional[Dict[str, Any]]:
        if not ctx.user_id:
            return {"status": "not_found", "message": "User is not authenticated."}
        return _resolve_whatsapp_recipient(whatsapp_service, db, ctx.user_id, args.get("contact", ""))

    async def handle_wa_send(args: Dict[str, Any], ctx: ToolContext) -> Dict[str, Any]:
        err = _require_user(ctx)
        if err:
            return err
        resolution = _resolve_whatsapp_recipient(whatsapp_service, db, ctx.user_id, args["contact"])
        if resolution.get("status") != "found":
            return resolution
        c = resolution["contact"]
        if not whatsapp_service:
            return {"status": "error", "error": "whatsapp_unavailable", "message": "WhatsApp service is unavailable."}
        res = await whatsapp_service.send_whatsapp_message(c["phone_e164"], args["message"], user_id=ctx.user_id)
        if res.get("status") == "success":
            return {
                "status": "success",
                "contact": c["display_name"],
                "message": f"Sent WhatsApp message to {c['display_name']}.",
            }
        return {
            "status": "error",
            "error": res.get("error", "send_failed"),
            "message": f"Failed to send WhatsApp message to {c['display_name']}: {res.get('error', 'unknown error')}",
        }

    registry.register(ToolSpec(
        name="whatsapp_send",
        description="Send a WhatsApp text message to a saved contact (name, alias like 'dad', or 'second number'). Requires exact match; non-trusted contacts ask confirmation.",
        parameters={
            "type": "object",
            "properties": {
                "contact": {"type": "string", "description": "Recipient name, alias, phone, or 'second number'."},
                "message": {"type": "string", "description": "Message body to send."},
            },
            "required": ["contact", "message"],
        },
        handler=handle_wa_send,
        risk=RISK_LOW,
        allowed_channels=("hud", "voice", "whatsapp"),
        timeout_s=15.0,
        parallel_safe=False,
        category="action",
        preflight=_preflight_wa_send,
    ))

    # 11. recall (Memory)
    async def handle_recall(args: Dict[str, Any], ctx: ToolContext) -> Dict[str, Any]:
        err = _require_user(ctx)
        if err:
            return err
        if memory_engine is None:
            return {"status": "error", "error": "memory_unavailable", "message": "Memory engine unavailable."}
        top_k = int(args.get("top_k", 5))
        return memory_engine.recall(ctx.user_id, args["query"], top_k=top_k)

    registry.register(ToolSpec(
        name="recall",
        description="Recall the user's personal facts, preferences, events, routines, or past knowledge from long-term memory.",
        parameters={
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Memory query."},
                "top_k": {"type": "integer", "minimum": 1, "maximum": 12},
            },
            "required": ["query"],
        },
        handler=handle_recall,
        risk=RISK_READ,
        allowed_channels=("hud", "voice", "whatsapp"),
        timeout_s=5.0,
        category="memory",
    ))

    # 12. create_reminder (Memory)
    async def handle_create_reminder(args: Dict[str, Any], ctx: ToolContext) -> Dict[str, Any]:
        err = _require_user(ctx)
        if err:
            return err
        if memory_engine is None:
            return {"status": "error", "error": "memory_unavailable", "message": "Memory engine unavailable."}
        return memory_engine.create_reminder(
            ctx.user_id,
            text=args["text"],
            due_at=args.get("due_at"),
            recurrence=args.get("recurrence"),
            channel=ctx.channel,
        )

    registry.register(ToolSpec(
        name="create_reminder",
        description="Schedule a reminder for the user with due time (ISO string or natural phrase like 'in 30 minutes' or '7pm').",
        parameters={
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "What to remind the user about."},
                "due_at": {"type": "string", "description": "Due date/time or natural phrase like 'in 15 minutes'."},
                "recurrence": {"type": "string", "enum": ["hourly", "daily", "weekly", "monthly"]},
            },
            "required": ["text", "due_at"],
        },
        handler=handle_create_reminder,
        risk=RISK_LOW,
        allowed_channels=("hud", "voice", "whatsapp"),
        timeout_s=5.0,
        category="memory",
    ))

    # 13. escalate (Tier 1 -> Tier 2 explicit handoff tool)
    async def handle_escalate(args: Dict[str, Any], ctx: ToolContext) -> Dict[str, Any]:
        return {"status": "escalate", "reason": args.get("reason", "needs_stronger_reasoning")}

    registry.register(ToolSpec(
        name="escalate",
        description="Escalate the current request to AIVA's deeper reasoning tier when complex planning or multi-source synthesis is required.",
        parameters={
            "type": "object",
            "properties": {
                "reason": {"type": "string", "description": "Short reason escalation is needed."},
            },
            "required": ["reason"],
        },
        handler=handle_escalate,
        risk=RISK_READ,
        allowed_channels=("hud", "voice", "whatsapp"),
        timeout_s=2.0,
        parallel_safe=False,
        category="conversation",
    ))

    return registry
