from core.agents.base_agent import BaseAgent


class BrowserAgent(BaseAgent):

    def __init__(
        self,
        browser_service=None,
        **kwargs
    ):

        super().__init__(

            name="browser_agent",

            **kwargs

        )


        self.browser_service = (
            browser_service
        )


        self.capabilities = [

            "browser_control",
            "navigation",
            "web_interaction"

        ]


        self.subscriptions = [

            "browser_url_changed",
            "goal.open_url",
            "browser.navigate"

        ]


    async def initialize(self):

        self.state["status"] = "ready"


        print(
            "[BrowserAgent] Initialized"
        )


    async def handle_event(
        self,
        event
    ):

        event_type = event.get(
            "type"
        )


        print(
            f"[BrowserAgent] Event: {event_type}"
        )


        # =====================================
        # OPEN URL GOAL
        # =====================================

        if event_type == "goal.open_url":

            data = event.get(
                "data",
                {}
            )


            url = data.get(
                "url"
            )


            if url:

                await self.open_url(
                    url
                )


        # =====================================
        # NAVIGATION EVENT
        # =====================================

        elif event_type == "browser.navigate":

            data = event.get(
                "data",
                {}
            )


            url = data.get(
                "url"
            )


            if url:

                await self.open_url(
                    url
                )


        # =====================================
        # URL CHANGED EVENT
        # =====================================

        elif event_type == "browser_url_changed":

            data = event.get(
                "data",
                {}
            )


            url = data.get(
                "url"
            )


            print(
                f"[BrowserAgent] Current URL: {url}"
            )


    async def execute(
        self,
        task
    ):

        print(
            f"[BrowserAgent] Executing task: {task}"
        )


        action = task.get(
            "action"
        )


        if action == "open_url":

            url = (

                task.get("url")

                or

                task.get(
                    "parameters",
                    {}
                ).get(
                    "url"
                )

            )


            if url:

                return await self.open_url(
                    url
                )


        return None


    async def open_url(
        self,
        url
    ):

        print(
            f"[BrowserAgent] Opening URL: {url}"
        )


        result = await (

            self.browser_service
            .open_url(
                url
            )

        )


        await self.emit_event(

            "browser.navigation.completed",

            {
                "url": url
            }

        )


        return result