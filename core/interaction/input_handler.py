"""Unified interaction pipeline routing user turns across MemoryEngine, LLM, and Planner."""

from typing import Callable, Optional


class InputHandler:

    def __init__(
        self,
        event_bus,
        intent_router_service,
        conversation_service,
        memory_engine,
        context_resolver_service=None,
        get_active_user_id: Optional[Callable[[], Optional[str]]] = None,
    ):
        self.event_bus = event_bus
        self.intent_router_service = intent_router_service
        self.conversation_service = conversation_service
        self.memory_engine = memory_engine
        self.context_resolver_service = context_resolver_service
        self.get_active_user_id = get_active_user_id
        self.voice_service = None
        self.pending_confirmation = None

    def _resolve_uid(self, user_id: Optional[str]) -> Optional[str]:
        if user_id:
            return user_id
        if self.get_active_user_id:
            return self.get_active_user_id()
        return None

    async def process_query(
        self,
        user_input: str,
        speak: bool = True,
        user_id: Optional[str] = None,
        channel: str = "voice",
        session_id: str = "default",
    ):
        user_input = (user_input or "").strip()
        uid = self._resolve_uid(user_id)

        if not user_input:
            return {
                "response": "",
                "should_continue": True,
                "should_end_session": False,
                "route": "empty",
            }

        if user_input.lower() in {"exit", "quit", "close aiva"}:
            return {
                "response": "Goodbye! Have a great day.",
                "should_continue": False,
                "should_end_session": True,
                "route": "exit",
            }

        # =====================================
        # VOICE CONFIRMATION FOR DESTRUCTIVE ACTIONS
        # =====================================
        if self.pending_confirmation:
            action_info = self.pending_confirmation
            self.pending_confirmation = None
            clean_in = user_input.lower().strip().rstrip(".!?")
            if clean_in in ("confirm", "yes confirm", "proceed", "yes proceed", "yes do it", "do it"):
                answer = f"Confirmed. Executing {action_info['label']} now."
                if speak and self.voice_service:
                    await self.voice_service.speak(answer)
                if self.memory_engine and uid:
                    self.memory_engine.observe(uid, {"user": user_input, "assistant": answer, "channel": channel}, channel=channel, session_id=session_id)
                await self.event_bus.emit(
                    {
                        "type": "goal.created",
                        "data": {
                            "goal": action_info["original_query"],
                            "intent": "device_action",
                            "user_id": uid,
                            "channel": channel,
                        },
                    }
                )
                return {
                    "response": answer,
                    "should_continue": True,
                    "should_end_session": False,
                    "route": "task",
                }
            answer = "Action cancelled. Standing by."
            if speak and self.voice_service:
                await self.voice_service.speak(answer)
            if self.memory_engine and uid:
                self.memory_engine.observe(uid, {"user": user_input, "assistant": answer, "channel": channel}, channel=channel, session_id=session_id)
            return {
                "response": answer,
                "should_continue": True,
                "should_end_session": False,
                "route": "conversation",
            }

        # Check destructive workstation command confirmation
        lower_in = user_input.lower().strip()
        destructive_keywords = ["shutdown", "shut down", "restart my pc", "reboot my pc", "delete all"]
        if any(dk in lower_in for dk in destructive_keywords):
            label = "shut down" if "shut" in lower_in else ("restart" if "re" in lower_in else "destructive operation")
            self.pending_confirmation = {
                "action": "destructive",
                "label": label,
                "original_query": user_input,
            }
            answer = f"This will {label} your workstation. Say 'confirm' to proceed."
            if speak and self.voice_service:
                await self.voice_service.speak(answer)
            if self.memory_engine and uid:
                self.memory_engine.observe(uid, {"user": user_input, "assistant": answer, "channel": channel}, channel=channel, session_id=session_id)
            return {
                "response": answer,
                "should_continue": True,
                "should_end_session": False,
                "expect_followup": True,
                "followup_timeout": 8.0,
                "route": "confirmation_required",
            }

        # =====================================
        # PENDING MEMORY CONFIRMATION / DUE TIME
        # =====================================
        if self.memory_engine and uid:
            if self.memory_engine.has_pending_confirmation(uid):
                res = self.memory_engine.handle_memory_intent(uid, user_input, channel=channel)
                answer = res["response"]
                if speak and self.voice_service:
                    await self.voice_service.speak(answer)
                return {
                    "response": answer,
                    "should_continue": True,
                    "should_end_session": False,
                    "route": "memory",
                }
            resolved_time_reply = self.memory_engine.extractor.try_resolve_pending_commitment_time(
                uid, user_input, channel=channel
            )
            if resolved_time_reply:
                if speak and self.voice_service:
                    await self.voice_service.speak(resolved_time_reply)
                return {
                    "response": resolved_time_reply,
                    "should_continue": True,
                    "should_end_session": False,
                    "route": "memory",
                }

        # =====================================
        # ROUTE INPUT
        # =====================================
        recent_context = (
            self.memory_engine.get_working_messages(uid, session_id=session_id, limit=4)
            if self.memory_engine and uid
            else []
        )
        route_result = await self.intent_router_service.route(user_input, recent_context)
        route = route_result.get("route", "conversation")
        subtype = route_result.get("subtype")

        # =====================================
        # SESSION CLOSE / DISMISSAL
        # =====================================
        if route == "session_close":
            name = (self.memory_engine.get_user_display_name(uid) if self.memory_engine else None) or "there"
            clean_in = user_input.lower().strip().rstrip("!?.,")
            if any(w in clean_in for w in ("thank", "thx")):
                answer = f"You're very welcome, {name}! Call me whenever you need anything."
            elif any(w in clean_in for w in ("sleep", "standby")):
                answer = f"Going to standby now, {name}. Say 'Hey AIVA' whenever you need me."
            elif any(w in clean_in for w in ("bye", "goodbye", "goodnight", "night")):
                answer = f"Talk to you soon, {name}! Have a wonderful time."
            else:
                answer = f"Got it, {name}! Going to standby."

            if speak and self.voice_service:
                await self.voice_service.speak(answer)
            if self.memory_engine and uid:
                self.memory_engine.observe(uid, {"user": user_input, "assistant": answer, "channel": channel}, channel=channel, session_id=session_id)
            return {
                "response": answer,
                "should_continue": False,
                "should_end_session": True,
                "expect_followup": False,
                "followup_timeout": 0.0,
                "route": "session_close",
            }

        # =====================================
        # MEMORY INTENT (RECALL / REMEMBER / FORGET / REMINDERS)
        # =====================================
        if route in ("memory", "memory_write", "memory_read") and self.memory_engine:
            res = self.memory_engine.handle_memory_intent(uid, user_input, channel=channel)
            answer = res.get("response", "Done.")
            if speak and self.voice_service:
                await self.voice_service.speak(answer)
            # Record non-sensitive conversational trace only when appropriate
            if uid and not res.get("confirmation_required"):
                self.memory_engine.observe(
                    uid,
                    {"user": user_input, "assistant": answer, "channel": channel},
                    channel=channel,
                    session_id=session_id,
                )
            return {
                "response": answer,
                "should_continue": True,
                "should_end_session": False,
                "expect_followup": bool(res.get("needs_due_time") or res.get("confirmation_required")),
                "followup_timeout": 9.0,
                "route": "memory",
            }

        # =====================================
        # BEFORE LLM: FAST FENCED MEMORY CONTEXT + WORKING HISTORY
        # =====================================
        raw_display_name = (
            self.memory_engine.get_user_display_name(uid)
            if self.memory_engine and uid and hasattr(self.memory_engine, "get_user_display_name")
            else None
        )
        display_name = (
            raw_display_name.strip()
            if isinstance(raw_display_name, str) and raw_display_name.strip()
            else None
        )
        model_history = (
            self.memory_engine.get_working_messages(uid, session_id=session_id, limit=6)
            if self.memory_engine and uid
            else []
        )
        user_context = (
            self.memory_engine.build_context(
                uid,
                user_input,
                channel=channel,
                authoritative_name=display_name,
            )
            if self.memory_engine and uid
            else ""
        )

        # =====================================
        # CONVERSATION
        # =====================================
        if route == "conversation":
            answer = await self.conversation_service.respond(
                user_input,
                model_history,
                user_context,
                user_name=display_name,
                user_id=uid,
                channel=channel,
            )

            if self.memory_engine and uid:
                # Non-blocking observe + one-time due-time nudge if commitment lacks time
                self.memory_engine.observe(
                    uid,
                    {"user": user_input, "assistant": answer, "channel": channel},
                    channel=channel,
                    session_id=session_id,
                )
                due_q = self.memory_engine.extractor.get_pending_commitment_question(uid)
                if due_q and due_q not in answer:
                    answer = f"{answer} {due_q}"

            if speak and self.voice_service:
                await self.voice_service.speak(answer)

            ends_with_q = answer.strip().endswith("?")
            return {
                "response": answer,
                "should_continue": True,
                "should_end_session": False,
                "expect_followup": True,
                "followup_timeout": 10.0 if ends_with_q else 7.0,
                "route": "conversation",
            }

        # =====================================
        # TASK / WORKFLOW EXECUTION
        # =====================================
        if subtype == "web_automation":
            task_ack = "Connecting directly to the portal to fetch your results now."
        else:
            task_ack = await self.conversation_service.respond(
                user_input,
                model_history,
                user_context,
                user_name=display_name,
                user_id=uid,
                channel=channel,
            )

        if speak and self.voice_service:
            await self.voice_service.speak(task_ack)

        if self.memory_engine and uid:
            self.memory_engine.observe(
                uid,
                {"user": user_input, "assistant": task_ack, "channel": channel},
                channel=channel,
                session_id=session_id,
            )

        await self.event_bus.emit(
            {
                "type": "goal.created",
                "data": {
                    "goal": user_input,
                    "intent": subtype,
                    "user_id": uid,
                    "channel": channel,
                },
            }
        )

        return {
            "response": task_ack,
            "should_continue": True,
            "should_end_session": False,
            "expect_followup": True,
            "followup_timeout": 30.0 if subtype == "web_automation" else 6.5,
            "route": "task",
        }

    async def process_input(
        self,
        user_input: str,
        user_id: Optional[str] = None,
        channel: str = "voice",
        speak: bool = True,
    ):
        result = await self.process_query(
            user_input,
            speak=speak,
            user_id=user_id,
            channel=channel,
        )
        answer = result.get("response", "")
        if answer:
            try:
                print(f"\nAIVA: {answer}\n")
            except UnicodeEncodeError:
                import sys

                sys.stdout.buffer.write(f"\nAIVA: {answer}\n\n".encode("utf-8"))
                sys.stdout.buffer.flush()
        return result.get("should_continue", True)
