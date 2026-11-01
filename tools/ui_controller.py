import time
import asyncio
import platform
import subprocess
import pyperclip
import pyautogui
from core.shared.logger import get_logger

logger = get_logger("UIController")

# Disable PyAutoGUI fail-safe pause for high-speed execution, but keep safety
pyautogui.PAUSE = 0.05
pyautogui.FAILSAFE = True


def focus_window(title_pattern: str) -> dict:
    """
    Brings the window matching title_pattern to the foreground and focuses it.
    """
    title_pattern = title_pattern.lower().strip()
    current_os = platform.system()

    if current_os == "Windows":
        try:
            import pygetwindow as gw
            all_windows = gw.getAllWindows()
            target_window = None

            for win in all_windows:
                if win.title and title_pattern in win.title.lower():
                    target_window = win
                    break

            if target_window:
                if target_window.isMinimized:
                    target_window.restore()
                target_window.activate()
                time.sleep(0.4)
                return {"status": "success", "message": f"Focused window: {target_window.title}"}
        except Exception as e:
            logger.warning(f"pygetwindow focus failed, trying ctypes: {e}")

        # Fallback to ctypes / win32gui
        try:
            import ctypes
            user32 = ctypes.windll.user32
            
            def enum_handler(hwnd, extra):
                if user32.IsWindowVisible(hwnd):
                    length = user32.GetWindowTextLengthW(hwnd)
                    if length > 0:
                        buff = ctypes.create_unicode_buffer(length + 1)
                        user32.GetWindowTextW(hwnd, buff, length + 1)
                        if title_pattern in buff.value.lower():
                            extra.append((hwnd, buff.value))
                return True

            WNDENUMPROC = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_int, ctypes.POINTER(ctypes.c_void_p))
            matches = []
            user32.EnumWindows(WNDENUMPROC(enum_handler), ctypes.py_object(matches))

            if matches:
                hwnd, win_title = matches[0]
                user32.ShowWindow(hwnd, 9)  # SW_RESTORE
                user32.SetForegroundWindow(hwnd)
                time.sleep(0.4)
                return {"status": "success", "message": f"Focused window via Win32: {win_title}"}
        except Exception as error:
            logger.error(f"Win32 focus error: {error}")

    return {"status": "warning", "message": f"Window matching '{title_pattern}' not found"}


def type_text(text: str, instant_paste: bool = True) -> dict:
    """
    Types or pastes text into the active input.
    Using clipboard (Ctrl+V) provides instant, emoji-safe, multilingual pasting.
    """
    if not text:
        return {"status": "error", "message": "No text provided"}

    try:
        if instant_paste:
            pyperclip.copy(text)
            time.sleep(0.08)
            pyautogui.keyDown('ctrl')
            time.sleep(0.05)
            pyautogui.press('v')
            time.sleep(0.05)
            pyautogui.keyUp('ctrl')
            time.sleep(0.1)
        else:
            pyautogui.write(text, interval=0.03)

        return {"status": "success", "message": f"Typed text: {text[:30]}..."}
    except Exception as error:
        logger.error(f"Error typing text: {error}")
        return {"status": "error", "message": str(error)}


def press_key(key: str, count: int = 1) -> dict:
    """
    Presses a specific keyboard key (enter, tab, esc, backspace, etc.).
    """
    key = str(key).lower().strip()
    key_map = {
        "return": "enter",
        "escape": "esc",
        "spacebar": "space",
        "delete": "del"
    }
    normalized_key = key_map.get(key, key)

    try:
        for _ in range(max(1, count)):
            pyautogui.press(normalized_key)
            time.sleep(0.12)
        return {"status": "success", "message": f"Pressed key '{normalized_key}' {count} time(s)"}
    except Exception as error:
        logger.error(f"Error pressing key '{key}': {error}")
        return {"status": "error", "message": str(error)}


def hotkey(*keys) -> dict:
    """
    Executes a keyboard hotkey combination with deliberate hold duration for browser/React listeners.
    """
    if not keys:
        return {"status": "error", "message": "No keys provided"}

    if len(keys) == 1:
        if isinstance(keys[0], (list, tuple)):
            keys = tuple(keys[0])
        elif isinstance(keys[0], str) and ("+" in keys[0] or "-" in keys[0]):
            delimiter = "+" if "+" in keys[0] else "-"
            keys = tuple([k.strip() for k in keys[0].split(delimiter)])

    key_map = {
        "/": "/",
        "slash": "/",
        "esc": "esc",
        "escape": "esc",
        "enter": "enter",
        "return": "enter"
    }
    cleaned_keys = [key_map.get(str(k).lower().strip(), str(k).lower().strip()) for k in keys]

    try:
        # Press all keys in sequence
        for k in cleaned_keys:
            pyautogui.keyDown(k)
            time.sleep(0.04)
        time.sleep(0.08)
        # Release in reverse order
        for k in reversed(cleaned_keys):
            pyautogui.keyUp(k)
            time.sleep(0.04)
        time.sleep(0.1)
        return {"status": "success", "message": f"Executed hotkey: {'+'.join(cleaned_keys)}"}
    except Exception as error:
        logger.error(f"Error executing hotkey '{keys}': {error}")
        return {"status": "error", "message": str(error)}


async def async_wait(seconds: float) -> dict:
    """
    Asynchronously waits for the specified duration to let apps/pages load.
    """
    duration = max(0.1, float(seconds))
    await asyncio.sleep(duration)
    return {"status": "success", "message": f"Waited {duration}s"}
