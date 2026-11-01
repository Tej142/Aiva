import os
import time
import subprocess
import platform
from pathlib import Path
from core.shared.config_loader import ensure_dir
from core.shared.logger import get_logger

logger = get_logger("SystemController")

def set_volume(level: int) -> str:
    """Sets system volume level (0 to 100)."""
    try:
        level = max(0, min(100, level))
        if platform.system() == "Windows":
            # Use PowerShell nircmd / WScript for volume or Windows CoreAudio
            ps_cmd = f"(New-Object -ComObject WScript.Shell).SendKeys([char]174)*50; (New-Object -ComObject WScript.Shell).SendKeys([char]175)*{int(level//2)}"
            subprocess.run(["powershell", "-c", ps_cmd], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return f"System volume set to {level}%."
    except Exception as err:
        logger.error(f"Volume error: {err}")
        return f"Failed to set volume: {err}"

def change_volume(delta: int) -> str:
    """Increases or decreases system volume by delta percentage."""
    try:
        key = "[char]175" if delta > 0 else "[char]174"
        count = abs(delta) // 2 or 1
        if platform.system() == "Windows":
            ps_cmd = f"(New-Object -ComObject WScript.Shell).SendKeys('{key}'*{count})"
            subprocess.run(["powershell", "-c", ps_cmd], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        action_word = "Increased" if delta > 0 else "Decreased"
        return f"Volume {action_word} successfully."
    except Exception as err:
        logger.error(f"Change volume error: {err}")
        return f"Failed to change volume: {err}"

def toggle_mute() -> str:
    """Toggles system audio mute state."""
    try:
        if platform.system() == "Windows":
            ps_cmd = "(New-Object -ComObject WScript.Shell).SendKeys([char]173)"
            subprocess.run(["powershell", "-c", ps_cmd], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return "Audio mute toggled."
    except Exception as err:
        return f"Failed to toggle mute: {err}"

def media_control(action: str) -> str:
    """Controls media playback: play_pause, next, prev."""
    action = action.lower().strip()
    key_code_map = {
        "play_pause": "[char]179",
        "play": "[char]179",
        "pause": "[char]179",
        "next": "[char]176",
        "prev": "[char]177",
        "previous": "[char]177"
    }
    char_code = key_code_map.get(action, "[char]179")
    try:
        if platform.system() == "Windows":
            ps_cmd = f"(New-Object -ComObject WScript.Shell).SendKeys('{char_code}')"
            subprocess.run(["powershell", "-c", ps_cmd], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return f"Media control '{action}' executed."
    except Exception as err:
        return f"Failed to execute media control: {err}"

def take_screenshot() -> str:
    """Captures desktop screenshot and saves it."""
    try:
        import pyautogui
        screenshots_dir = ensure_dir("scratch/screenshots")
        filename = f"screenshot_{int(time.time())}.png"
        filepath = os.path.join(screenshots_dir, filename)
        pyautogui.screenshot(filepath)
        return f"Screenshot saved to {filepath}"
    except ImportError:
        # Fallback to PIL ImageGrab
        try:
            from PIL import ImageGrab
            screenshots_dir = ensure_dir("scratch/screenshots")
            filename = f"screenshot_{int(time.time())}.png"
            filepath = os.path.join(screenshots_dir, filename)
            img = ImageGrab.grab()
            img.save(filepath)
            return f"Screenshot saved to {filepath}"
        except Exception as err:
            return f"Screenshot capture error: {err}"
    except Exception as err:
        return f"Screenshot error: {err}"

def system_power(action: str) -> str:
    """Executes system power / lock action: lock, sleep, battery."""
    action = action.lower().strip()
    try:
        if action == "lock":
            if platform.system() == "Windows":
                subprocess.run("rundll32.exe user32.dll,LockWorkStation", shell=True)
            return "Workstation locked."
        elif action == "battery":
            import psutil
            battery = psutil.sensors_battery()
            if battery:
                status = "Plugged in" if battery.power_plugged else "Discharging"
                return f"Battery is at {battery.percent}% ({status})."
            return "Battery information unavailable."
        return f"System power action '{action}' acknowledged."
    except Exception as err:
        return f"System power action error: {err}"
