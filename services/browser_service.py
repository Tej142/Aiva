from selenium import webdriver
from selenium.webdriver.chrome.options import Options


class BrowserService:

    def __init__(self):

        self.driver = None

        self.active = False


    # =====================================
    # START BROWSER ONLY WHEN REQUIRED
    # =====================================

    async def start(self):

        # Already running
        if self.driver is not None:

            try:

                _ = self.driver.current_url

                return

            except Exception:

                self.driver = None

                self.active = False


        print(
            "[BrowserService] Starting browser..."
        )


        options = Options()

        options.add_argument(
            "--start-maximized"
        )

        options.add_argument(
            "--disable-dev-shm-usage"
        )

        options.add_argument(
            "--no-sandbox"
        )

        # 1. Remove the "Chrome is being controlled by automated test software" banner
        options.add_experimental_option(
            "excludeSwitches",
            ["enable-automation"]
        )

        options.add_experimental_option(
            "useAutomationExtension",
            False
        )

        # 2. Disable Blink automation flags to avoid reCAPTCHA / bot detection
        options.add_argument(
            "--disable-blink-features=AutomationControlled"
        )

        options.add_argument(
            "--disable-infobars"
        )

        options.add_argument(
            "user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
        )

        self.driver = webdriver.Chrome(
            options=options
        )

        # 3. Patch navigator.webdriver property via CDP
        try:
            self.driver.execute_cdp_cmd(
                "Page.addScriptToEvaluateOnNewDocument",
                {
                    "source": """
                        Object.defineProperty(navigator, 'webdriver', {
                            get: () => undefined
                        });
                        window.navigator.chrome = {
                            runtime: {},
                            loadTimes: function() {},
                            csi: function() {},
                            app: {}
                        };
                    """
                }
            )
        except Exception:
            pass

        self.active = True


        print(
            "[BrowserService] Browser started"
        )


    # =====================================
    # CHECK BROWSER STATE
    # =====================================

    def is_running(self):

        if self.driver is None:

            return False


        try:

            _ = self.driver.current_url

            return True

        except Exception:

            self.driver = None

            self.active = False

            return False


    # =====================================
    # OPEN URL
    # =====================================

    async def open_url(
        self,
        url,
        reuse_existing: bool = True
    ):
        import webbrowser
        import platform
        import os
        from tools import ui_controller

        if not url.startswith("http://") and not url.startswith("https://"):
            url = "https://" + url

        domain_keywords = {
            "web.whatsapp.com": "WhatsApp",
            "whatsapp.com": "WhatsApp",
            "youtube.com": "YouTube",
            "instagram.com": "Instagram",
            "mail.google.com": "Gmail",
            "gmail.com": "Gmail",
            "github.com": "GitHub",
            "chatgpt.com": "ChatGPT"
        }

        target_keyword = None
        for dom, kw in domain_keywords.items():
            if dom in url.lower():
                target_keyword = kw
                break

        # If already open, simply focus and reuse the existing window/tab
        if reuse_existing and target_keyword:
            focus_res = ui_controller.focus_window(target_keyword)
            if focus_res.get("status") == "success":
                print(
                    f"[BrowserService] Reused existing {target_keyword} window/tab without creating a duplicate."
                )
                return {
                    "status": "success",
                    "url": url,
                    "reused_existing": True
                }

        print(
            f"[BrowserService] Opening new tab: {url}"
        )

        try:
            if platform.system() == "Windows":
                os.startfile(url)
            else:
                webbrowser.open(url)
        except Exception:
            webbrowser.open(url)

        return {
            "status": "success",
            "url": url,
            "reused_existing": False
        }


    # =====================================
    # BACKWARD COMPATIBILITY
    # =====================================

    async def open(
        self,
        url
    ):

        return await self.open_url(
            url
        )


    # =====================================
    # GET PAGE TITLE
    # =====================================

    async def get_title(self):

        if not self.is_running():

            return None


        try:

            return self.driver.title

        except Exception:

            return None


    # =====================================
    # GET CURRENT URL
    # =====================================

    async def get_url(self):

        if not self.is_running():

            return None


        try:

            return self.driver.current_url

        except Exception:

            return None


    # =====================================
    # GET TABS
    # =====================================

    async def get_tabs(self):

        if not self.is_running():

            return []


        try:

            return self.driver.window_handles

        except Exception:

            return []


    # =====================================
    # SWITCH TAB
    # =====================================

    async def switch_tab(
        self,
        index
    ):

        tabs = await self.get_tabs()


        if (
            index >= 0
            and
            index < len(tabs)
        ):

            self.driver.switch_to.window(

                tabs[index]

            )


    # =====================================
    # PAGE SOURCE
    # =====================================

    async def get_page_source(self):

        if not self.is_running():

            return None


        try:

            return self.driver.page_source

        except Exception:

            return None


    # =====================================
    # STOP BROWSER
    # =====================================

    async def stop(self):

        if self.driver is None:

            return


        try:

            self.driver.quit()

        except Exception as error:

            print(
                "[BrowserService Stop Error]",
                error
            )


        finally:

            self.driver = None

            self.active = False


        print(
            "[BrowserService] Browser stopped"
        )


    # =====================================
    # CAPABILITY ACTION
    # =====================================

    async def open_url_action(
        self,
        action
    ):

        url = action.get(
            "url"
        ) or action.get(
            "parameters", {}
        ).get("url")


        if not url:

            return {

                "status": "error",

                "message": "URL missing"

            }


        return await self.open_url(
            url
        )