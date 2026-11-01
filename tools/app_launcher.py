import os
import re
import platform
import subprocess
from tools.app_discovery import find_best_app_match
from core.shared.config_loader import load_json
from core.shared.logger import get_logger

logger = get_logger("AppLauncher")

def get_config_path() -> str:
    current_os = platform.system()
    if current_os == "Windows":
        return "config/windows_apps.json"
    elif current_os == "Linux":
        return "config/linux_apps.json"
    elif current_os == "Darwin":
        return "config/mac_apps.json"
    return ""

def load_apps() -> dict:
    path = get_config_path()
    if not path:
        logger.warning("Unsupported OS for app config")
        return {}
    return load_json(path, default={})

def run_command(command: str) -> bool:
    try:
        command = command.strip()
        current_os = platform.system()

        if current_os == "Windows":
            # Remove any redundant leading 'start '
            clean_cmd = re.sub(r"^start\s+(\"\"\s+)?", "", command).strip()
            # Strip quotes if it's a single quoted file path
            if (clean_cmd.startswith('"') and clean_cmd.endswith('"')) or (clean_cmd.startswith("'") and clean_cmd.endswith("'")):
                unquoted = clean_cmd[1:-1]
                if os.path.exists(unquoted) or clean_cmd.endswith(":"):
                    clean_cmd = unquoted

            # If it's a path or protocol (e.g. ms-settings:), use native os.startfile (no CMD window)
            if os.path.exists(clean_cmd) or ":" in clean_cmd:
                os.startfile(clean_cmd)
                return True

            # Otherwise launch with CREATE_NO_WINDOW so no black console appears
            creation_flags = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
            subprocess.Popen(
                clean_cmd,
                shell=True,
                creationflags=creation_flags,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL
            )
        else:
            subprocess.Popen(
                command.split(),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL
            )
        return True
    except Exception as error:
        logger.error(f"App launch error for '{command}': {error}")
        return False

def open_application(app_name: str) -> str:
    app_name = app_name.lower().strip()
    apps = load_apps()

    # STEP 1 → Config apps lookup
    if app_name in apps:
        command = apps[app_name]
        if run_command(command):
            return f"{app_name.capitalize()} opened successfully."
        return f"Failed to open {app_name}."

    # STEP 2 → Windows dynamic discovery
    if platform.system() == "Windows":
        matched_name, matched_path = find_best_app_match(app_name)
        if matched_name and matched_path:
            try:
                os.startfile(matched_path)
                return f"{matched_name.capitalize()} opened dynamically."
            except Exception as error:
                return f"[DYNAMIC LAUNCH ERROR] {error}"

    return f"Application '{app_name}' not found."