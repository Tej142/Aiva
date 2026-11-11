"""Channel-aware response shaping and sentence segmentation for streamed TTS."""

import re
from typing import Any, Dict, List, Optional

from services.voice_service import sanitize_for_speech
from services.whatsapp_service import format_for_whatsapp

_ABBREV_RE = re.compile(r"\b(Mr|Mrs|Ms|Dr|Prof|Sr|Jr|St|vs|etc|e\.g|i\.e)\.", re.IGNORECASE)
_URL_RE = re.compile(r"https?://[^\s]+", re.IGNORECASE)
_SENTENCE_BOUNDARY_RE = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9\u0c00-\u0c7f\"'(\[])")


def _split_masked_sentences(text: str) -> List[str]:
    """Split text at sentence boundaries while preserving URLs and common abbreviations."""
    placeholders: Dict[str, str] = {}

    def _stash(match):
        tok = f"__AIVA_PH_{len(placeholders)}__"
        placeholders[tok] = match.group(0)
        return tok

    masked = _ABBREV_RE.sub(_stash, _URL_RE.sub(_stash, text))
    parts = _SENTENCE_BOUNDARY_RE.split(masked)
    out: List[str] = []
    for part in parts:
        for tok, orig in placeholders.items():
            part = part.replace(tok, orig)
        out.append(part)
    return out


def split_sentences(text: str) -> List[str]:
    """Split prose cleanly into spoken sentences for low-latency chunked Edge TTS."""
    clean = (text or "").strip()
    if not clean:
        return []
    out = [p.strip() for p in _split_masked_sentences(clean) if p and p.strip()]
    return out or [clean]


def pop_complete_sentences(buffer: str) -> tuple[List[str], str]:
    """Extract finalized sentences from a streaming token buffer, keeping trailing fragment."""
    if not buffer:
        return [], ""
    if buffer.count("```") % 2 == 1:
        return [], buffer

    parts = _split_masked_sentences(buffer)
    if len(parts) <= 1:
        if "\n" in buffer:
            idx = buffer.rfind("\n")
            head = buffer[:idx].strip()
            tail = buffer[idx + 1:]
            if head and head[-1] in ".!?":
                return [s for s in split_sentences(head) if s], tail
        return [], buffer

    ready = [p.strip() for p in parts[:-1] if p and p.strip()]
    return ready, parts[-1]


def shape_for_voice(text: str, max_sentences: int = 2, max_chars: int = 260) -> str:
    """Shape any response into at most `max_sentences` crisp natural spoken sentences."""
    spoken = sanitize_for_speech(text or "", max_chars=max(max_chars, 260))
    sents = split_sentences(spoken)
    if not sents:
        return "Done."
    kept = sents[:max(1, int(max_sentences))]
    result = " ".join(kept).strip()
    if len(sents) > max_sentences and len(result) > 40:
        if result[-1] not in ".!?":
            result += "."
    return result


def normalize_sources(sources: Optional[List[Dict[str, Any]]]) -> List[Dict[str, str]]:
    """Normalize source citations to `{title, url, retrieved_at}` dicts."""
    clean: List[Dict[str, str]] = []
    seen_urls = set()
    for s in sources or []:
        if not isinstance(s, dict):
            continue
        url = str(s.get("url") or "").strip()
        title = str(s.get("title") or url or "Source").strip()
        retrieved_at = str(s.get("retrieved_at") or "").strip()
        key = url or title
        if not key or key in seen_urls:
            continue
        seen_urls.add(key)
        clean.append({"title": title, "url": url, "retrieved_at": retrieved_at})
    return clean[:8]


def shape_response(
    text: str,
    channel: str = "hud",
    sources: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """Shape agent output per target channel (`hud`, `voice`, `whatsapp`, `alexa`)."""
    raw = (text or "").strip()
    chan = (channel or "hud").strip().lower()
    norm_sources = normalize_sources(sources)

    if chan == "voice":
        spoken = shape_for_voice(raw, max_sentences=2, max_chars=260)
        return {
            "text": spoken,
            "speech_text": spoken,
            "sources": norm_sources,
            "channel": chan,
        }

    if chan == "whatsapp":
        wa_body = format_for_whatsapp(raw)
        if norm_sources:
            top_links = [s["url"] for s in norm_sources[:3] if s.get("url")]
            if top_links and not any(u in wa_body for u in top_links):
                wa_body = format_for_whatsapp(
                    f"{wa_body}\n\nSources:\n" + "\n".join(f"- {u}" for u in top_links)
                )
        return {
            "text": wa_body,
            "speech_text": "",
            "sources": norm_sources,
            "channel": chan,
        }

    # HUD / default: rich Markdown in `text` + concise <=2-sentence `speech_text` + structured sources
    speech = shape_for_voice(raw, max_sentences=2, max_chars=260)
    return {
        "text": raw,
        "speech_text": speech,
        "sources": norm_sources,
        "channel": chan,
    }
