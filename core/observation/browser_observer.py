import asyncio


class BrowserObserver:

    def __init__(
        self,
        browser_service,
        event_bus
    ):

        self.browser = (
            browser_service
        )

        self.event_bus = (
            event_bus
        )

        self.last_url = None

        self.running = False


    async def start(self):

        self.running = True


        print(
            "[BrowserObserver] Started"
        )


        while self.running:

            try:

                # Browser not running yet
                if not self.browser.is_running():

                    await asyncio.sleep(
                        1
                    )

                    continue


                current_url = await (

                    self.browser
                    .get_url()

                )


                if not current_url:

                    await asyncio.sleep(
                        1
                    )

                    continue


                if current_url == self.last_url:

                    await asyncio.sleep(
                        1
                    )

                    continue


                self.last_url = (
                    current_url
                )


                await self.event_bus.emit(

                    "browser_url_changed",

                    {
                        "url":
                        current_url
                    }

                )


                print(

                    "[BrowserObserver] "
                    f"URL changed: {current_url}"

                )


            except Exception as error:

                print(

                    "[BrowserObserver Error]",

                    error

                )


            await asyncio.sleep(
                1
            )


    async def stop(self):

        self.running = False


        print(
            "[BrowserObserver] Stopped"
        )