import os
import json
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent.parent

def get_path(*relative_parts) -> Path:
    """Returns an absolute Path relative to the AIVA project root."""
    return BASE_DIR.joinpath(*relative_parts)

def ensure_dir(relative_or_abs_path) -> Path:
    """Ensures that the directory for the given path exists."""
    path = Path(relative_or_abs_path)
    if not path.is_absolute():
        path = get_path(path)
    if path.suffix:  # It's a file path
        path.parent.mkdir(parents=True, exist_ok=True)
    else:
        path.mkdir(parents=True, exist_ok=True)
    return path

def load_json(relative_path: str, default=None):
    """Safely loads JSON from a path relative to project root."""
    abs_path = get_path(relative_path)
    if abs_path.exists():
        try:
            with open(abs_path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as err:
            print(f"[ConfigLoader Error] Failed loading {relative_path}: {err}")
    return default if default is not None else {}

def save_json(relative_path: str, data, indent=4) -> bool:
    """Safely saves data as JSON to a path relative to project root."""
    abs_path = get_path(relative_path)
    abs_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with open(abs_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=indent, ensure_ascii=False)
        return True
    except Exception as err:
        print(f"[ConfigLoader Error] Failed saving {relative_path}: {err}")
        return False

def load_secrets() -> dict:
    """Loads secrets.json with environment variable fallback support."""
    secrets = load_json("config/secrets.json", default={})
    
    # Environment variable fallbacks
    if "groq_api_key" not in secrets or not secrets["groq_api_key"]:
        secrets["groq_api_key"] = os.getenv("GROQ_API_KEY", "")
    if "mistral_api_key" not in secrets or not secrets["mistral_api_key"]:
        secrets["mistral_api_key"] = os.getenv("MISTRAL_API_KEY", "")
    if "gemini_api_key" not in secrets or not secrets["gemini_api_key"]:
        secrets["gemini_api_key"] = os.getenv("GEMINI_API_KEY", "")
    if "tavily_api_key" not in secrets or not secrets["tavily_api_key"]:
        secrets["tavily_api_key"] = os.getenv("TAVILY_API_KEY", "")
        
    return secrets
