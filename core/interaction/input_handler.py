class InputHandler:

    def __init__(
        self,
        event_bus,
        intent_router_service,
        conversation_service,
        conversation_memory_service,
        context_resolver_service,
        user_memory_service,
        memory_extraction_service,
        passive_memory_service=None
    ):

        self.event_bus = event_bus
        self.intent_router_service = intent_router_service
        self.conversation_service = conversation_service
        self.conversation_memory_service = conversation_memory_service
        self.context_resolver_service = context_resolver_service
        self.user_memory_service = user_memory_service
        self.memory_extraction_service = memory_extraction_service
        self.passive_memory_service = passive_memory_service
        self.pending_memory = None


    async def process_query(
        self,
        user_input,
        speak=True
    ):
        user_input = user_input.strip()

        if not user_input:
            return {
                "response": "",
                "should_continue": True,
                "should_end_session": False,
                "route": "empty"
            }

        if user_input.lower() in {
            "exit",
            "quit",
            "close aiva"
        }:
            return {
                "response": "Goodbye! Have a great day.",
                "should_continue": False,
                "should_end_session": True,
                "route": "exit"
            }

        # =====================================
        # COMPLETE PENDING MEMORY
        # =====================================
        if self.pending_memory:
            key = self.pending_memory["key"]
            value = user_input.strip().rstrip(".!?")

            if not value:
                answer = "I didn't catch that. Could you tell me again?"
                return {
                    "response": answer,
                    "should_continue": True,
                    "should_end_session": False,
                    "route": "memory_write"
                }

            self.user_memory_service.remember(key, value)
            self.pending_memory = None
            answer = self._build_memory_answer(key, value)
            self.conversation_memory_service.add_turn(user_input, answer)
            return {
                "response": answer,
                "should_continue": True,
                "should_end_session": False,
                "route": "memory_write"
            }

        # =====================================
        # ROUTE INPUT
        # =====================================
        recent_context = self.conversation_memory_service.get_history(limit=3)
        route_result = await self.intent_router_service.route(
            user_input,
            recent_context
        )

        route = route_result.get("route", "conversation")
        subtype = route_result.get("subtype")
        source = route_result.get("source")

        print(f"[InputHandler] Route: {route}")
        print(f"[InputHandler] Intent: {subtype}")
        print(f"[InputHandler] Router: {source}")

        # =====================================
        # SESSION CLOSE / DISMISSAL
        # =====================================
        if route == "session_close":
            name = "Charan"
            try:
                name = self.user_memory_service.get("name") or "Charan"
            except Exception:
                pass
            clean_in = user_input.lower().strip().rstrip("!?.,")
            if any(w in clean_in for w in ["thank", "thx"]):
                answer = f"You're very welcome, {name}! Call me whenever you need anything."
            elif any(w in clean_in for w in ["sleep", "standby"]):
                answer = f"Going to standby now, {name}. Say 'Hey AIVA' whenever you need me."
            elif any(w in clean_in for w in ["bye", "goodbye", "goodnight", "night"]):
                answer = f"Talk to you soon, {name}! Have a wonderful time."
            else:
                answer = f"Got it, {name}! Going to standby."

            if speak and hasattr(self, 'voice_service') and self.voice_service:
                await self.voice_service.speak(answer)

            self.conversation_memory_service.add_turn(user_input, answer)
            return {
                "response": answer,
                "should_continue": False,
                "should_end_session": True,
                "expect_followup": False,
                "followup_timeout": 0.0,
                "route": "session_close"
            }

        # =====================================
        # PERSISTENT MEMORY WRITE
        # =====================================
        if route == "memory_write":
            extracted = self.memory_extraction_service.extract(user_input)

            if not extracted:
                answer = "I understand that you want me to remember something, but what exactly should I remember?"
                self.conversation_memory_service.add_turn(user_input, answer)
                if speak and hasattr(self, 'voice_service') and self.voice_service:
                    await self.voice_service.speak(answer)
                return {
                    "response": answer,
                    "should_continue": True,
                    "should_end_session": False,
                    "expect_followup": True,
                    "followup_timeout": 9.0,
                    "route": "memory_write"
                }

            status = extracted.get("status")

            if status == "incomplete":
                self.pending_memory = {"key": extracted["key"]}
                answer = extracted["question"]
                self.conversation_memory_service.add_turn(user_input, answer)
                if speak and hasattr(self, 'voice_service') and self.voice_service:
                    await self.voice_service.speak(answer)
                return {
                    "response": answer,
                    "should_continue": True,
                    "should_end_session": False,
                    "expect_followup": True,
                    "followup_timeout": 10.0,
                    "route": "memory_write"
                }

            key = extracted["key"]
            value = extracted["value"]
            self.user_memory_service.remember(key, value)
            answer = self._build_memory_answer(key, value)
            self.conversation_memory_service.add_turn(user_input, answer)
            if speak and hasattr(self, 'voice_service') and self.voice_service:
                await self.voice_service.speak(answer)
            return {
                "response": answer,
                "should_continue": True,
                "should_end_session": False,
                "expect_followup": True,
                "followup_timeout": 6.0,
                "route": "memory_write"
            }

        # =====================================
        # BUILD CONTEXT & HISTORY
        # =====================================
        history = self.conversation_memory_service.get_history(limit=6)
        model_history = []
        for turn in history:
            user_message = turn.get("user")
            aiva_message = turn.get("aiva")
            if user_message:
                model_history.append({"role": "user", "content": user_message})
            if aiva_message:
                model_history.append({"role": "assistant", "content": aiva_message})

        user_context = self.user_memory_service.build_context()

        # =====================================
        # CONVERSATION
        # =====================================
        if route == "conversation":
            answer = await self.conversation_service.respond(
                user_input,
                model_history,
                user_context
            )

            if speak and hasattr(self, 'voice_service') and self.voice_service:
                await self.voice_service.speak(answer)

            self.conversation_memory_service.add_turn(user_input, answer)

            # Silently learn any facts the user revealed — fire and forget
            if self.passive_memory_service:
                try:
                    import asyncio as _aio
                    _aio.ensure_future(
                        self.passive_memory_service.extract_and_save_async(user_input)
                    )
                except RuntimeError:
                    pass  # no running loop in test context — safe to skip

            # Did AIVA ask a question or is it an interactive conversational dialogue?
            ends_with_q = answer.strip().endswith("?")
            followup_timeout = 10.0 if ends_with_q else 7.0

            return {
                "response": answer,
                "should_continue": True,
                "should_end_session": False,
                "expect_followup": True,
                "followup_timeout": followup_timeout,
                "route": "conversation"
            }

        # =====================================
        # TASK
        # =====================================

        # For web automation tasks, give a concise, specific acknowledgement
        if subtype == "web_automation":
            task_ack = (
                "Connecting directly to the portal to fetch your results now."
            )
        else:
            task_ack = await self.conversation_service.respond(
                user_input,
                model_history,
                user_context
            )

        if speak and hasattr(self, 'voice_service') and self.voice_service:
            await self.voice_service.speak(task_ack)

        self.conversation_memory_service.add_turn(user_input, task_ack)

        await self.event_bus.emit({
            "type": "goal.created",
            "data": {
                "goal": user_input,
                "intent": subtype
            }
        })

        return {
            "response": task_ack,
            "should_continue": True,
            "should_end_session": False,
            "expect_followup": True,
            "followup_timeout": 30.0 if subtype == "web_automation" else 6.5,
            "route": "task"
        }

    async def process_input(
        self,
        user_input
    ):
        result = await self.process_query(user_input, speak=True)
        answer = result.get("response", "")
        if answer:
            try:
                print(f"\nAIVA: {answer}\n")
            except UnicodeEncodeError:
                import sys
                sys.stdout.buffer.write(f"\nAIVA: {answer}\n\n".encode('utf-8'))
                sys.stdout.buffer.flush()

        return result.get("should_continue", True)

    # =====================================
    # MEMORY RESPONSE BUILDER
    # =====================================

    def _build_memory_answer(self, key, value):
        """Warm, friend-like acknowledgement when saving a user fact."""
        name = self.user_memory_service.get("name", "")
        name_suffix = f", {name}" if name and key != "name" else ""

        responses = {
            "name":       f"Nice to officially know your name{name_suffix}! I'll remember that.",
            "likes":      f"Got it{name_suffix} — noted that you like {value}.",
            "dislikes":   f"Noted{name_suffix}, I'll remember you're not into {value}.",
            "preference": f"Alright{name_suffix}, I'll keep that preference in mind.",
            "mood":       f"Noted. I'll remember how you're feeling.",
            "age":        f"Got it{name_suffix}, noted that down.",
            "location":   f"Cool{name_suffix}, I'll remember you're from {value}.",
            "profession": f"Nice{name_suffix}, noted that you're a {value}.",
            "goal":       f"Alright{name_suffix}, I'll keep that in mind.",
        }

        return responses.get(key, f"Got it{name_suffix}, I'll remember: {value}.")