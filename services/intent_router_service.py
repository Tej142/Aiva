class IntentRouterService:

    def __init__(
        self,
        intent_classifier_service=None
    ):

        self.intent_classifier_service = (
            intent_classifier_service
        )


        self.memory_phrases = (

            "remember ",
            "remember that ",
            "don't forget ",
            "dont forget ",
            "save this ",
            "keep in mind "

        )


        self.action_phrases = (

            "open ",
            "launch ",
            "close ",
            "play ",
            "start ",
            "stop ",
            "navigate to ",
            "go to "

        )


        self.research_phrases = (

            "research ",
            "research about ",
            "find information about ",
            "search about ",
            "investigate ",
            "look up ",
            "do research on "

        )


        self.simple_conversation = {

            "hi",
            "hii",
            "hiii",
            "hello",
            "hey",
            "yo",
            "good morning",
            "good afternoon",
            "good evening",
            "good night"

        }

        self.dismissal_phrases = {
            # Thanks variants
            "thanks",
            "thank you",
            "thanks aiva",
            "thank you aiva",
            "thank u",
            "thx",
            # Completion
            "that's all",
            "thats all",
            "that is all",
            "that's it",
            "thats it",
            "that is it",
            "nothing else",
            "no thanks",
            "no that's all",
            "no thats all",
            "no that's it",
            "i'm done",
            "im done",
            "done",
            "all good",
            "all done",
            # Goodbye
            "bye",
            "goodbye",
            "bye bye",
            "good night",
            "goodnight",
            "see you",
            "see ya",
            "cya",
            # Sleep / Standby
            "stop listening",
            "go to sleep",
            "sleep now",
            "sleep",
            "go to standby",
            "aiva sleep",
            "aiva stop",
            "ok stop",
            # Dismissal
            "never mind",
            "nevermind",
            "nvm",
            "stop"
        }


    async def route(
        self,
        user_input,
        conversation_context=None
    ):

        text = (
            user_input
            .lower()
            .strip()
        )


        clean_text = (
            text
            .rstrip("!?.,")
        )

        # =====================================
        # FAST PATH: EXTERNAL URL + FORM DATA
        # =====================================
        # If user provides an http/https URL along with additional context
        # (credentials, form fields, IDs) → always a web automation task.
        import re as _re
        _url_match = _re.search(r"https?://\S+", user_input, _re.IGNORECASE)
        if _url_match:
            # Extra words beyond the URL = form data / instructions
            _rest = user_input[_url_match.end():].strip()
            if _rest:  # has context beyond just the URL → web automation
                return {
                    "route": "task",
                    "subtype": "web_automation",
                    "confidence": 1.0,
                    "source": "local"
                }
            else:
                # Bare URL only → simple open_url
                return {
                    "route": "task",
                    "subtype": "open_url",
                    "confidence": 1.0,
                    "source": "local"
                }

        # =====================================
        # FAST LOCAL DISMISSAL / SESSION CLOSE
        # =====================================
        if clean_text in self.dismissal_phrases:
            return {
                "route": "session_close",
                "subtype": "dismissal",
                "confidence": 1.0,
                "source": "local"
            }


        # =====================================
        # FAST LOCAL MEMORY WRITE
        # =====================================

        if clean_text.startswith(
            self.memory_phrases
        ):

            return {

                "route": "memory_write",

                "subtype": "memory_write",

                "confidence": 1.0,

                "source": "local"

            }


        # =====================================
        # FAST LOCAL ACTION
        # =====================================

        if clean_text.startswith(
            self.action_phrases
        ):

            return {

                "route": "task",

                "subtype": "action",

                "confidence": 1.0,

                "source": "local"

            }


        # =====================================
        # FAST LOCAL RESEARCH
        # =====================================

        if clean_text.startswith(
            self.research_phrases
        ):

            return {

                "route": "task",

                "subtype": "research",

                "confidence": 1.0,

                "source": "local"

            }


        # =====================================
        # FAST LOCAL CONVERSATION
        # =====================================

        if clean_text in self.simple_conversation:

            return {

                "route": "conversation",

                "subtype": "conversation",

                "confidence": 1.0,

                "source": "local"

            }


        # =====================================
        # GEMINI INTENT CLASSIFICATION
        # =====================================

        if self.intent_classifier_service:

            intent = await (

                self.intent_classifier_service
                .classify(

                    user_input,

                    conversation_context

                )

            )


            print(
                f"[IntentRouter] AI Intent: {intent}"
            )


            # ---------------------------------
            # CONVERSATION
            # ---------------------------------

            if intent == "conversation":

                return {

                    "route": "conversation",

                    "subtype": "conversation",

                    "confidence": 0.9,

                    "source": "gemini"

                }


            # ---------------------------------
            # MEMORY WRITE
            # ---------------------------------

            if intent == "memory_write":

                return {

                    "route": "memory_write",

                    "subtype": "memory_write",

                    "confidence": 0.9,

                    "source": "gemini"

                }


            # ---------------------------------
            # MEMORY READ
            # ---------------------------------

            if intent == "memory_read":

                return {

                    "route": "conversation",

                    "subtype": "memory_read",

                    "confidence": 0.9,

                    "source": "gemini"

                }


            # ---------------------------------
            # TASK INTENTS
            # ---------------------------------

            if intent in {

                "knowledge",
                "research",
                "action"

            }:

                return {

                    "route": "task",

                    "subtype": intent,

                    "confidence": 0.9,

                    "source": "gemini"

                }


        # =====================================
        # SAFE FALLBACK
        # =====================================

        return {

            "route": "conversation",

            "subtype": "conversation",

            "confidence": 0.5,

            "source": "fallback"

        }