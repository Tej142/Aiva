"""Versionable system prompts and few-shot examples for AIVA Agent Core."""

from pathlib import Path
from typing import Dict

_PROMPTS_DIR = Path(__file__).resolve().parent
_CACHED_PROMPTS: Dict[str, str] = {}


def load_prompt_file(filename: str) -> str:
    """Load and cache a markdown prompt file from core/agent/prompts/."""
    if filename not in _CACHED_PROMPTS:
        p = _PROMPTS_DIR / filename
        if p.exists():
            _CACHED_PROMPTS[filename] = p.read_text(encoding="utf-8").strip()
        else:
            _CACHED_PROMPTS[filename] = ""
    return _CACHED_PROMPTS[filename]


def get_tier1_prompt_template() -> str:
    return load_prompt_file("tier1.md")


def get_tier2_prompt_template() -> str:
    return load_prompt_file("tier2.md")


def get_examples_content() -> str:
    return load_prompt_file("examples.md")


def clear_prompt_cache() -> None:
    _CACHED_PROMPTS.clear()


clear_prompts_cache = clear_prompt_cache

