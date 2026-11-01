from selenium import webdriver

from selenium.webdriver.chrome.service import Service

import time
import subprocess
import platform


driver = None


def get_chrome_command():

    current_os = platform.system()

    # WINDOWS
    if current_os == "Windows":

        return (
            r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe"
        )

    # LINUX
    elif current_os == "Linux":

        return "google-chrome"

    # MAC
    elif current_os == "Darwin":

        return (
            "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
        )

    return None


def launch_debug_chrome():

    chrome_command = get_chrome_command()

    if not chrome_command:

        raise Exception("Unsupported OS")

    current_os = platform.system()

    # WINDOWS
    if current_os == "Windows":

        user_data_dir = r"D:\aiva\chrome_debug"

    # LINUX / MAC
    else:

        user_data_dir = "/tmp/aiva_chrome_debug"

    command = [
        chrome_command,
        "--remote-debugging-port=9222",
        f"--user-data-dir={user_data_dir}",
        "--disable-blink-features=AutomationControlled",
        "--disable-infobars",
        "--no-first-run",
        "--no-default-browser-check",
        "--start-maximized",
        "--user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
    ]

    subprocess.Popen(command)

    time.sleep(3)


def start_browser():

    global driver

    if driver is not None:

        try:

            driver.title

            return driver

        except Exception:

            driver = None

    def create_driver():
        options = webdriver.ChromeOptions()
        options.debugger_address = "127.0.0.1:9222"
        options.add_experimental_option("excludeSwitches", ["enable-automation"])
        options.add_experimental_option("useAutomationExtension", False)
        options.add_argument("--disable-blink-features=AutomationControlled")
        options.add_argument("--disable-infobars")
        
        drv = webdriver.Chrome(options=options)
        try:
            drv.execute_cdp_cmd(
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
        return drv

    try:

        driver = create_driver()
        return driver

    except Exception:

        print(
            "\n[BROWSER] Launching debug Chrome automatically..."
        )

        launch_debug_chrome()

        driver = create_driver()
        return driver


import webbrowser
import urllib.parse
import os

def open_website(url, reuse_existing: bool = True):
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

    if reuse_existing and target_keyword:
        focus_res = ui_controller.focus_window(target_keyword)
        if focus_res.get("status") == "success":
            return f"Switched to existing {target_keyword} window/tab"

    try:
        if platform.system() == "Windows":
            os.startfile(url)
        else:
            webbrowser.open(url)
        return f"Opened {url}"
    except Exception:
        webbrowser.open(url)
        return f"Opened {url}"


def open_new_tab(url):
    return open_website(url)


def google_search(query):
    encoded_query = urllib.parse.quote_plus(query)
    search_url = f"https://www.google.com/search?q={encoded_query}"
    open_website(search_url)
    return f"Searched Google for: {query}"