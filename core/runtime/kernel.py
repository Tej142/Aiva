import asyncio


from core.runtime.event_bus import EventBus
from core.runtime.state_manager import StateManager
from core.runtime.task_queue import TaskQueue
from core.runtime.scheduler import Scheduler
from core.runtime.service_registry import ServiceRegistry

from core.execution.action_dispatcher import ActionDispatcher
from core.execution.capability_registry import CapabilityRegistry

from core.observation.browser_observer import BrowserObserver

from core.planning.workflow_engine import WorkflowEngine

from core.agents.agent_manager import AgentManager
from core.agents.planner_agent import PlannerAgent
from core.agents.observer_agent import ObserverAgent
from core.agents.browser_agent import BrowserAgent
from core.agents.memory_agent import MemoryAgent
from core.agents.research_agent import ResearchAgent

from core.interaction.input_handler import InputHandler
from core.interaction.chat_runtime import ChatRuntime

from services.browser_service import BrowserService
from services.memory_service import MemoryService
from services.session_service import SessionService

from services.mistral_service import MistralService
from services.tavily_service import TavilyService
from services.knowledge_service import KnowledgeService

from services.recall_service import RecallService
from services.reasoning_service import ReasoningService

from services.context_resolver_service import (
    ContextResolverService
)

from services.conversation_memory_service import (
    ConversationMemoryService
)

from services.conversation_service import (
    ConversationService
)

from services.intent_router_service import (
    IntentRouterService
)

from services.user_memory_service import (
    UserMemoryService
)

from services.task_pattern_service import (
    TaskPatternService
)

from core.observation.workflow_observer import (
    WorkflowObserver
)

from services.memory_extraction_service import (
    MemoryExtractionService
)

from services.intent_classifier_service import (
    IntentClassifierService
)

from services.wake_word_service import (
    WakeWordService
)

from services.outcome_reporter_service import (
    OutcomeReporterService
)

from services.passive_memory_service import (
    PassiveMemoryService
)

from services.web_automation_service import (
    WebAutomationService
)


class AIVAKernel:

    def __init__(self):

        self.running = False


        # =====================================
        # RUNTIME COMPONENTS
        # =====================================

        self.event_bus = EventBus()

        self.state = StateManager()

        self.task_queue = TaskQueue()

        self.scheduler = Scheduler()

        self.registry = ServiceRegistry()

        self.capabilities = CapabilityRegistry()

        from services.database_service import DatabaseService
        from services.device_manager import DeviceManager
        from services.device_channel import DeviceChannel

        self.db = DatabaseService()
        self.device_manager = DeviceManager(self.db)
        self.device_channel = DeviceChannel(self.db, self.device_manager)
        self.active_user_id = None


        # =====================================
        # CORE SERVICES
        # =====================================

        self.browser_service = (
            BrowserService()
        )

        self.web_automation_service = (
            WebAutomationService()
        )

        self.memory_service = (
            MemoryService()
        )

        self.session_service = (
            SessionService()
        )


        # =====================================
        # WORKFLOW SYSTEM
        # =====================================

        self.workflow_engine = (

            WorkflowEngine(

                self.event_bus,

                self.task_queue

            )

        )


        self.action_dispatcher = (

            ActionDispatcher(

                self.capabilities

            )

        )


        # =====================================
        # BROWSER OBSERVER
        # Browser does NOT start here
        # =====================================

        self.browser_observer = (

            BrowserObserver(

                self.browser_service,

                self.event_bus

            )

        )


        # =====================================
        # AI SERVICES
        # =====================================

        self.mistral_service = (
            MistralService()
        )


        self.tavily_service = (
            TavilyService()
        )


        self.conversation_service = (
            ConversationService()
        )

        self.intent_classifier_service = (
            IntentClassifierService()
        )

        self.user_memory_service = (
            UserMemoryService()
        )


        self.memory_extraction_service = (
            MemoryExtractionService()
        )


        # =====================================
        # KNOWLEDGE SERVICE
        # =====================================

        self.knowledge_service = (

            KnowledgeService(

                self.memory_service,

                self.tavily_service,

                self.mistral_service

            )

        )


        # =====================================
        # MEMORY SERVICES
        # =====================================

        self.recall_service = (
            RecallService()
        )


        self.reasoning_service = (

            ReasoningService(

                self.mistral_service

            )

        )


        self.conversation_memory_service = (
            ConversationMemoryService()
        )


        # =====================================
        # INTELLIGENCE ROUTING
        # =====================================

        self.intent_router_service = (

            IntentRouterService(

                intent_classifier_service=(
                    self.intent_classifier_service
                )

            )

        )


        self.context_resolver_service = (

            ContextResolverService(

                self.conversation_memory_service

            )

        )


        self.task_pattern_service = (
            TaskPatternService()
        )

        self.workflow_observer = (
            WorkflowObserver(
                self.event_bus,
                self.task_pattern_service
            )
        )

        self.wake_word_service = (
            WakeWordService(self)
        )

        self.outcome_reporter = (
            OutcomeReporterService()
        )

        self.passive_memory_service = (
            PassiveMemoryService(
                user_memory_service=self.user_memory_service
            )
        )

        # =====================================
        # AGENT MANAGER
        # =====================================

        self.agent_manager = (
            AgentManager()
        )


        # =====================================
        # PLANNER AGENT
        # =====================================

        self.planner_agent = (

            PlannerAgent(

                event_bus=self.event_bus,

                state_manager=self.state,

                memory_service=(
                    self.memory_service
                ),

                mistral_service=(
                    self.mistral_service
                ),

                recall_service=(
                    self.recall_service
                ),

                reasoning_service=(
                    self.reasoning_service
                ),

                conversation_memory_service=(
                    self.conversation_memory_service
                ),

                context_resolver_service=(
                    self.context_resolver_service
                ),

                task_pattern_service=(
                    self.task_pattern_service
                )

            )

        )


        # =====================================
        # OBSERVER AGENT
        # =====================================

        self.observer_agent = (

            ObserverAgent(

                event_bus=self.event_bus,

                state_manager=self.state,

                memory_service=(
                    self.memory_service
                )

            )

        )


        # =====================================
        # BROWSER AGENT
        # =====================================

        self.browser_agent = (

            BrowserAgent(

                browser_service=(
                    self.browser_service
                ),

                event_bus=self.event_bus,

                state_manager=self.state,

                memory_service=(
                    self.memory_service
                )

            )

        )


        # =====================================
        # MEMORY AGENT
        # =====================================

        self.memory_agent = (

            MemoryAgent(

                event_bus=self.event_bus,

                state_manager=self.state,

                memory_service=(
                    self.memory_service
                )

            )

        )


        # =====================================
        # RESEARCH AGENT
        # =====================================

        self.research_agent = (

            ResearchAgent(

                event_bus=self.event_bus,

                state_manager=self.state,

                memory_service=(
                    self.memory_service
                ),

                knowledge_service=(
                    self.knowledge_service
                ),

                conversation_memory_service=(
                    self.conversation_memory_service
                )

            )

        )


        # =====================================
        # INTERACTION LAYER
        # =====================================

        self.input_handler = (

            InputHandler(

                event_bus=self.event_bus,

                intent_router_service=(
                    self.intent_router_service
                ),

                conversation_service=(
                    self.conversation_service
                ),

                conversation_memory_service=(
                    self.conversation_memory_service
                ),

                context_resolver_service=(
                    self.context_resolver_service
                ),

                user_memory_service=(
                    self.user_memory_service
                ),

                memory_extraction_service=(
                    self.memory_extraction_service
                ),

                passive_memory_service=(
                    self.passive_memory_service
                )

            )

        )


        self.chat_runtime = (

            ChatRuntime(

                self.input_handler,

                self

            )

        )


    async def initialize_runtime(self):
        print(
            "[AIVA] Starting runtime kernel..."
        )

        self.running = True

        self.state.set(
            "runtime_active",
            True
        )

        # =====================================
        # REGISTER SERVICES DICTIONARY
        # =====================================
        services = {
            "browser": self.browser_service,
            "memory": self.memory_service,
            "session": self.session_service,
            "workflow": self.workflow_engine,
            "dispatcher": self.action_dispatcher,
            "knowledge": self.knowledge_service,
            "recall": self.recall_service,
            "reasoning": self.reasoning_service,
            "conversation_memory": self.conversation_memory_service,
            "user_memory": self.user_memory_service,
            "task_pattern": self.task_pattern_service,
            "memory_extraction": self.memory_extraction_service,
            "intent_classifier": self.intent_classifier_service,
            "context_resolver": self.context_resolver_service,
            "intent_router": self.intent_router_service,
            "conversation": self.conversation_service
        }

        for name, service in services.items():
            self.registry.register(
                name,
                service
            )

        # =====================================
        # REGISTER CAPABILITIES
        # =====================================
        from services.voice_service import VoiceService
        self.voice_service = VoiceService()

        # Bind OutcomeReporter: give it voice + memory so it can speak results
        self.outcome_reporter.voice_service = self.voice_service
        self.outcome_reporter.conversation_memory_service = self.conversation_memory_service
        self.outcome_reporter.wake_word_service = self.wake_word_service
        self.workflow_engine.outcome_reporter = self.outcome_reporter

        from tools.app_launcher import open_application
        import platform

        def _get_active_uid(action_dict):
            uid = action_dict.get("user_id") or getattr(self, "active_user_id", None)
            if not uid:
                primary = self.db.get_primary_user()
                if primary:
                    uid = primary["id"]
            return uid

        async def open_url_action(action):
            uid = _get_active_uid(action)
            params = action.get("parameters", {})
            url = params.get("url") or action.get("url") or action.get("query") or ""
            target_dev = action.get("device_name") or params.get("device_name")

            if uid and self.device_channel.get_online_devices_for_user(uid):
                return await self.device_channel.dispatch_command(
                    user_id=uid,
                    action="open_url",
                    params={"url": url},
                    target_device_name=target_dev
                )

            if platform.system() != "Windows":
                msg = "No Windows device is currently online to open this URL. Please open your AIVA Device Agent on your computer."
                print(f"[DeviceChannel] {msg}")
                return {"status": "offline", "error": "device_offline", "result": msg, "message": msg}

            return await self.browser_service.open_url_action(action)

        self.capabilities.register(
            "open_url",
            open_url_action
        )

        self.capabilities.register(
            "research",
            self.research_agent.execute
        )

        from tools.doc_generator import create_word_document

        async def create_document_action(action):
            params = action.get("parameters", {})
            file_name = params.get("file_name") or action.get("file_name", "notes.docx")
            topic = params.get("topic") or action.get("topic", "")
            content = params.get("content") or action.get("content", "")
            res = create_word_document(file_name=file_name, topic=topic, content=content)
            print(f"[DocGenerator] {res.get('message', res)}")
            return res

        self.capabilities.register("create_document", create_document_action)

        async def launch_app_action(action):
            uid = _get_active_uid(action)
            app_name = action.get("app_name") or action.get("parameters", {}).get("app_name", "")
            target_dev = action.get("device_name") or action.get("parameters", {}).get("device_name")

            if uid and self.device_channel.get_online_devices_for_user(uid):
                return await self.device_channel.dispatch_command(
                    user_id=uid,
                    action="open_app",
                    params={"app_name": app_name},
                    target_device_name=target_dev
                )

            if platform.system() != "Windows":
                msg = f"No Windows device is online to launch '{app_name}'. Please connect your AIVA Device Agent."
                print(f"[DeviceChannel] {msg}")
                return {"status": "offline", "error": "device_offline", "result": msg, "message": msg}

            result_str = open_application(app_name)
            print(f"[AppLauncher] {result_str}")
            return {"status": "success", "result": result_str}

        self.capabilities.register(
            "launch_app",
            launch_app_action
        )

        from tools import system_controller

        async def adjust_volume_action(action):
            uid = _get_active_uid(action)
            params = action.get("parameters", {})
            act = params.get("action", action.get("action"))

            if uid and self.device_channel.get_online_devices_for_user(uid):
                act_name = "volume_mute" if act == "mute" else "volume_set"
                return await self.device_channel.dispatch_command(
                    user_id=uid,
                    action=act_name,
                    params=params,
                    target_device_name=action.get("device_name")
                )

            if platform.system() != "Windows":
                msg = "No Windows device is online to adjust volume. Please connect your AIVA Device Agent."
                return {"status": "offline", "error": "device_offline", "result": msg, "message": msg}

            if act == "mute":
                res = system_controller.toggle_mute()
            elif "level" in params:
                res = system_controller.set_volume(params["level"])
            else:
                res = system_controller.change_volume(params.get("delta", 10))
            print(f"[SystemController] {res}")
            return {"status": "success", "result": res}

        self.capabilities.register("adjust_volume", adjust_volume_action)

        async def media_control_action(action):
            uid = _get_active_uid(action)
            params = action.get("parameters", {})
            media_action = params.get("media_action", action.get("media_action", "play_pause"))

            if uid and self.device_channel.get_online_devices_for_user(uid):
                return await self.device_channel.dispatch_command(
                    user_id=uid,
                    action=f"media_{media_action}",
                    params=params,
                    target_device_name=action.get("device_name")
                )

            if platform.system() != "Windows":
                msg = "No Windows device is online for media control. Please connect your AIVA Device Agent."
                return {"status": "offline", "error": "device_offline", "result": msg, "message": msg}

            res = system_controller.media_control(media_action)
            print(f"[SystemController] {res}")
            return {"status": "success", "result": res}

        self.capabilities.register("media_control", media_control_action)

        async def take_screenshot_action(action):
            uid = _get_active_uid(action)
            if uid and self.device_channel.get_online_devices_for_user(uid):
                res = await self.device_channel.dispatch_command(
                    user_id=uid,
                    action="screenshot",
                    params={},
                    target_device_name=action.get("device_name")
                )
                if res.get("status") == "success" and "screenshot_base64" in res.get("output", {}):
                    import base64
                    raw_data = base64.b64decode(res["output"]["screenshot_base64"])
                    from core.shared.config_loader import ensure_dir
                    sc_dir = ensure_dir("storage")
                    sc_path = os.path.join(sc_dir, "last_screenshot.png")
                    with open(sc_path, "wb") as f:
                        f.write(raw_data)
                return res

            if platform.system() != "Windows":
                msg = "No Windows device is online to capture screenshot. Please connect your AIVA Device Agent."
                return {"status": "offline", "error": "device_offline", "result": msg, "message": msg}

            res = system_controller.take_screenshot()
            print(f"[SystemController] {res}")
            return {"status": "success", "result": res}

        self.capabilities.register("take_screenshot", take_screenshot_action)

        async def system_power_action(action):
            uid = _get_active_uid(action)
            params = action.get("parameters", {})
            p_action = params.get("power_action", action.get("power_action", "lock"))

            if uid and self.device_channel.get_online_devices_for_user(uid):
                return await self.device_channel.dispatch_command(
                    user_id=uid,
                    action="lock_screen" if p_action == "lock" else p_action,
                    params=params,
                    target_device_name=action.get("device_name")
                )

            if platform.system() != "Windows":
                msg = "No Windows device is online. Please connect your AIVA Device Agent."
                return {"status": "offline", "error": "device_offline", "result": msg, "message": msg}

            res = system_controller.system_power(p_action)
            print(f"[SystemController] {res}")
            return {"status": "success", "result": res}

        self.capabilities.register("system_power", system_power_action)

        async def speak_action(action):
            params = action.get("parameters", {})
            text = params.get("text", action.get("text", ""))
            await self.voice_service.speak(text)
            return {"status": "success"}

        # =====================================
        # UNIVERSAL UI & KEYBOARD AUTOMATION
        # =====================================
        from tools import ui_controller

        async def focus_window_action(action):
            params = action.get("parameters", {})
            title = params.get("title") or action.get("title", "")
            res = ui_controller.focus_window(title)
            print(f"[UIController] {res.get('message', res)}")
            return res

        async def type_text_action(action):
            uid = _get_active_uid(action)
            params = action.get("parameters", {})
            if uid and self.device_channel.get_online_devices_for_user(uid):
                return await self.device_channel.dispatch_command(
                    user_id=uid,
                    action="type_text",
                    params=params,
                    target_device_name=action.get("device_name")
                )
            text = params.get("text") or action.get("text", "")
            instant = params.get("instant_paste", True)
            res = ui_controller.type_text(text, instant_paste=instant)
            print(f"[UIController] {res.get('message', res)}")
            return res

        async def press_key_action(action):
            uid = _get_active_uid(action)
            params = action.get("parameters", {})
            if uid and self.device_channel.get_online_devices_for_user(uid):
                return await self.device_channel.dispatch_command(
                    user_id=uid,
                    action="press_key",
                    params=params,
                    target_device_name=action.get("device_name")
                )
            key = params.get("key") or action.get("key", "enter")
            count = params.get("count", 1)
            res = ui_controller.press_key(key, count=count)
            print(f"[UIController] {res.get('message', res)}")
            return res

        async def hotkey_action(action):
            uid = _get_active_uid(action)
            params = action.get("parameters", {})
            if uid and self.device_channel.get_online_devices_for_user(uid):
                return await self.device_channel.dispatch_command(
                    user_id=uid,
                    action="hotkey",
                    params=params,
                    target_device_name=action.get("device_name")
                )
            keys = params.get("keys") or action.get("keys", [])
            if isinstance(keys, str):
                keys = [k.strip() for k in keys.split("+")]
            res = ui_controller.hotkey(*keys)
            print(f"[UIController] {res.get('message', res)}")
            return res

        async def open_file_action(action):
            uid = _get_active_uid(action)
            params = action.get("parameters", {})
            if uid and self.device_channel.get_online_devices_for_user(uid):
                return await self.device_channel.dispatch_command(
                    user_id=uid,
                    action="open_file",
                    params=params,
                    target_device_name=action.get("device_name")
                )
            return {"status": "offline", "error": "device_offline", "result": "No Windows device is online."}

        async def list_running_apps_action(action):
            uid = _get_active_uid(action)
            params = action.get("parameters", {})
            if uid and self.device_channel.get_online_devices_for_user(uid):
                return await self.device_channel.dispatch_command(
                    user_id=uid,
                    action="list_running_apps",
                    params=params,
                    target_device_name=action.get("device_name")
                )
            return {"status": "offline", "error": "device_offline", "result": "No Windows device is online."}

        async def close_app_action(action):
            uid = _get_active_uid(action)
            params = action.get("parameters", {})
            if uid and self.device_channel.get_online_devices_for_user(uid):
                return await self.device_channel.dispatch_command(
                    user_id=uid,
                    action="close_app",
                    params=params,
                    target_device_name=action.get("device_name")
                )
            return {"status": "offline", "error": "device_offline", "result": "No Windows device is online."}

        self.capabilities.register("type_text", type_text_action)
        self.capabilities.register("press_key", press_key_action)
        self.capabilities.register("hotkey", hotkey_action)
        self.capabilities.register("open_file", open_file_action)
        self.capabilities.register("list_running_apps", list_running_apps_action)
        self.capabilities.register("close_app", close_app_action)

        async def wait_action(action):
            params = action.get("parameters", {})
            seconds = params.get("seconds") or action.get("seconds", 1.0)
            res = await ui_controller.async_wait(seconds)
            print(f"[UIController] {res.get('message', res)}")
            return res

        # =====================================
        # COMPUTER VISION & SCREEN UNDERSTANDING
        # =====================================
        from tools import vision_controller

        async def visual_click_action(action):
            params = action.get("parameters", {})
            target = params.get("target") or params.get("target_description") or action.get("target", "")
            act_type = params.get("action_type", "click")
            res = await vision_controller.visual_click(target, action_type=act_type)
            print(f"[VisionController] {res.get('message', res)}")
            return res

        async def visual_type_action(action):
            params = action.get("parameters", {})
            target = params.get("target") or params.get("target_description") or action.get("target", "")
            text = params.get("text") or action.get("text", "")
            press_enter = params.get("press_enter", True)
            res = await vision_controller.visual_type(target, text, press_enter=press_enter)
            print(f"[VisionController] {res.get('message', res)}")
            return res

        async def inspect_screen_action(action):
            params = action.get("parameters", {})
            query = params.get("query") or params.get("question") or action.get("query", "Describe what is currently visible on the screen.")
            res = await vision_controller.inspect_screen(query)
            analysis = res.get("analysis", "")
            print(f"[VisionController] Screen analysis: {analysis}")
            if self.voice_service and analysis:
                await self.voice_service.speak(analysis)
            return res

        self.capabilities.register("visual_click", visual_click_action)
        self.capabilities.register("visual_type", visual_type_action)
        self.capabilities.register("inspect_screen", inspect_screen_action)

        # ─── Web Automation ────────────────────────────────────────────────
        async def web_automation_action(action):
            result = await self.web_automation_service.web_automation_action(action)
            summary = result.get("summary", "I completed the web task.")
            # Speak result back to user
            if self.voice_service and summary:
                await self.voice_service.speak(summary)
            # Also log it to conversation memory so AIVA remembers it
            if hasattr(self, "conversation_memory_service") and self.conversation_memory_service:
                goal = action.get("parameters", {}).get("instruction") or action.get("goal", "web task")
                self.conversation_memory_service.add_turn(goal, summary)
            return result

        self.capabilities.register("web_automation", web_automation_action)

        self.input_handler.voice_service = self.voice_service

        # =====================================
        # REGISTER AGENTS
        # =====================================
        agents = [
            self.planner_agent,
            self.observer_agent,
            self.browser_agent,
            self.memory_agent,
            self.research_agent
        ]

        for agent in agents:
            self.agent_manager.register(
                agent
            )

        # =====================================
        # EVENT SUBSCRIPTIONS
        # =====================================
        self.event_bus.on(
            "*",
            self.agent_manager.dispatch_event
        )

        self.event_bus.on(
            "browser_url_changed",
            self.handle_browser_change
        )

        self.event_bus.on(
            "workflow.generated",
            self.handle_generated_workflow
        )

        # =====================================
        # INITIALIZE AGENTS
        # =====================================
        await self.agent_manager.initialize_all()

        # =====================================
        # RECOVER WORKFLOWS
        # =====================================
        await self.workflow_engine.recover_workflows()

        self.workflow_engine.continuation_engine.restore()

        # =====================================
        # BACKGROUND TASKS
        # =====================================
        asyncio.create_task(
            self.browser_observer.start()
        )

        asyncio.create_task(
            self.workflow_observer.start()
        )

        asyncio.create_task(
            self.process_tasks()
        )

        asyncio.create_task(
            self.runtime_monitor_loop()
        )

        asyncio.create_task(
            self.runtime_loop()
        )

        print(
            "[AIVA] Runtime kernel started"
        )
        await asyncio.sleep(0.2)

    async def start(self, voice_mode: bool = False, all_voice_mode: bool = False, adaptive_voice: bool = True):
        await self.initialize_runtime()

        if all_voice_mode:
            self.wake_word_service.start_background(asyncio.get_running_loop(), mode="allvoice", continuous_mode=True)
        elif voice_mode:
            self.wake_word_service.start_background(asyncio.get_running_loop(), mode="wakeword", continuous_mode=False)
        elif adaptive_voice:
            self.wake_word_service.start_background(asyncio.get_running_loop(), mode="adaptive", continuous_mode=False)

        # =====================================
        # START CHAT
        # =====================================
        await self.chat_runtime.start()

    def start_voice_mode(self, mode: str = "adaptive", continuous: bool = False):
        """Activates voice detection (mode='adaptive', 'wakeword', or 'allvoice')."""
        self.wake_word_service.start_background(mode=mode, continuous_mode=continuous)

    def stop_voice_mode(self):
        """Deactivates background voice detection."""
        self.wake_word_service.stop()

    async def start_server(self):
        await self.initialize_runtime()
        return self


    async def runtime_loop(self):

        while self.running:

            await (

                self.scheduler
                .tick()

            )


            await asyncio.sleep(
                0.1
            )


    async def process_tasks(self):

        while self.running:

            task = await (

                self.task_queue
                .get_task()

            )


            if not task:

                continue


            print(
                f"[AIVA] Processing task: {task}"
            )


            self.memory_service.remember(

                "last_task",

                task

            )


            try:

                await (

                    self.action_dispatcher
                    .dispatch(
                        task
                    )

                )

                print(
                    "\n[AIVA] Task completed successfully.\n"
                )

            except Exception as error:

                print(

                    "[Dispatcher Error]",

                    error

                )


    async def runtime_monitor_loop(self):

        while self.running:

            self.state.set(

                "active_agents",

                list(

                    self.agent_manager
                    .agents
                    .keys()

                )

            )


            await asyncio.sleep(
                5
            )


    async def handle_browser_change(
        self,
        data
    ):

        url = (

            data.get(
                "url"
            )

            or ""

        )


        print(
            f"[AIVA] URL changed: {url}"
        )


        if not url:

            return


        self.memory_service.remember(

            "last_url",

            url

        )


        if "youtube.com" in url:

            await self.event_bus.emit({

                "type":
                "youtube.detected",

                "data": {

                    "url":
                    url

                }

            })


    async def handle_generated_workflow(
        self,
        event
    ):

        data = event.get(

            "data",

            {}

        )


        workflow = data.get(
            "workflow"
        )


        if not workflow:

            print(
                "[AIVA] Empty workflow"
            )

            return


        print(
            "[AIVA] Workflow graph received"
        )


        await (

            self.workflow_engine
            .register_workflow_graph(

                workflow

            )

        )


    async def shutdown(self):

        if not self.running:

            return


        print(
            "[AIVA] Shutting down..."
        )


        self.running = False


        await (

            self.browser_observer
            .stop()

        )


        await (

            self.agent_manager
            .shutdown_all()

        )


        try:

            await (

                self.browser_service
                .stop()

            )


        except Exception as error:

            print(

                "[Browser Shutdown Error]",

                error

            )


        self.state.set(

            "runtime_active",

            False

        )


        print(
            "[AIVA] Runtime stopped"
        )