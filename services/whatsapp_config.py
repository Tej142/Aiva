"""Hot-reloading .env configuration, constant-time credential verification, and trigger utilities for WhatsApp."""

from dataclasses import dataclass
import hashlib
import hmac
import logging
import os
from pathlib import Path
import re
import secrets
from typing import Any, Dict, Optional, Tuple

from core.shared.config_loader import BASE_DIR

logger = logging.getLogger("aiva.whatsapp")

# Snapshot startup process environ AND startup .env contents so auth_service.load_dotenv()
# never freezes hot-reload or leaks host .env values into AIVA_ENV_FILE test sandboxes,
# while explicit runtime os.environ/patch.dict overrides still win.
_PROCESS_START_ENVIRON: Dict[str, str] = dict(os.environ)
_DOTENV_CACHE: Dict[str, Tuple[Tuple[int, int, int], Dict[str, str]]] = {}

_SENSITIVE_KEYS = frozenset({
    "AIVA_ACCESS_USERNAME",
    "AIVA_ACCESS_PASSWORD",
    "AIVA_ACCESS_PASSWORD_HASH",
    "WHATSAPP_BRIDGE_SECRET",
    "JWT_SECRET",
    "BREVO_API_KEY",
    "GROQ_API_KEY",
    "MISTRAL_API_KEY",
    "GEMINI_API_KEY",
    "TAVILY_API_KEY",
})


def normalize_phone(raw_phone: str) -> str:
    """Normalize a phone string or WhatsApp JID to E.164 digits only (no '+')."""
    if not raw_phone:
        return ""
    s = str(raw_phone).strip()
    if "@" in s:
        s = s.split("@", 1)[0]
    if ":" in s:
        s = s.split(":", 1)[0]
    digits = re.sub(r"\D", "", s)
    if digits.startswith("00") and len(digits) > 4:
        digits = digits[2:]
    return digits


def mask_phone(phone_or_jid: str) -> str:
    """Mask all digits except the last 4 (e.g. '+********3210' or '****')."""
    digits = normalize_phone(phone_or_jid)
    if len(digits) <= 4:
        return "****"
    return "+" + ("*" * (len(digits) - 4)) + digits[-4:]


def _decode_double_quoted(raw: str) -> str:
    out = []
    i = 0
    n = len(raw)
    while i < n:
        ch = raw[i]
        if ch == "\\" and i + 1 < n:
            nxt = raw[i + 1]
            if nxt == "n":
                out.append("\n")
            elif nxt == "r":
                out.append("\r")
            elif nxt == "t":
                out.append("\t")
            else:
                out.append(nxt)
            i += 2
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def parse_dotenv_text(text: str) -> Dict[str, str]:
    """Parse .env content supporting quoted values with inner spaces and escapes."""
    parsed: Dict[str, str] = {}
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].strip()
        if "=" not in line:
            continue
        key, rest = line.split("=", 1)
        key = key.strip()
        if not key or not re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", key):
            continue

        val_raw = rest.lstrip()
        if not val_raw:
            parsed[key] = ""
            continue

        if val_raw[0] == '"':
            # Scan until unescaped closing double quote
            i = 1
            n = len(val_raw)
            buf = []
            while i < n:
                ch = val_raw[i]
                if ch == "\\" and i + 1 < n:
                    buf.append(ch)
                    buf.append(val_raw[i + 1])
                    i += 2
                    continue
                if ch == '"':
                    break
                buf.append(ch)
                i += 1
            parsed[key] = _decode_double_quoted("".join(buf))
        elif val_raw[0] == "'":
            end_idx = val_raw.find("'", 1)
            if end_idx == -1:
                parsed[key] = val_raw[1:]
            else:
                parsed[key] = val_raw[1:end_idx]
        else:
            # Strip trailing inline comment preceded by whitespace
            unquoted = re.split(r"\s+#", val_raw, maxsplit=1)[0].strip()
            parsed[key] = unquoted

    return parsed


def get_env_file_path() -> Path:
    override = os.environ.get("AIVA_ENV_FILE", "").strip()
    if override:
        return Path(override)
    return Path(BASE_DIR) / ".env"


def _load_dotenv_cached(env_path: Optional[Path] = None, *, force: bool = False) -> Dict[str, str]:
    target = env_path or get_env_file_path()
    cache_key = str(target)
    try:
        st = target.stat()
        sig = (1, st.st_mtime_ns, st.st_size)
    except OSError:
        _DOTENV_CACHE.pop(cache_key, None)
        return {}

    cached = _DOTENV_CACHE.get(cache_key)
    if not force and cached is not None and cached[0] == sig:
        return cached[1]

    try:
        content = target.read_text(encoding="utf-8")
        data = parse_dotenv_text(content)
    except OSError:
        data = {}
    _DOTENV_CACHE[cache_key] = (sig, data)
    return data


def clear_dotenv_cache() -> None:
    _DOTENV_CACHE.clear()


def _load_default_dotenv_startup() -> Dict[str, str]:
    default_env = Path(BASE_DIR) / ".env"
    try:
        if default_env.exists():
            return parse_dotenv_text(default_env.read_text(encoding="utf-8"))
    except OSError:
        pass
    return {}


_DEFAULT_DOTENV_STARTUP: Dict[str, str] = _load_default_dotenv_startup()


def _resolve_key(key: str, dotenv_map: Dict[str, str], default: str = "") -> str:
    cur_env = os.environ.get(key)
    start_env = _PROCESS_START_ENVIRON.get(key)
    dotenv_startup_val = _DEFAULT_DOTENV_STARTUP.get(key)
    # Explicit runtime/test override (not injected by python-dotenv from startup .env) takes highest precedence
    if cur_env is not None and cur_env != start_env and cur_env != dotenv_startup_val:
        return cur_env
    # Otherwise active .env file (or AIVA_ENV_FILE) wins over startup environment so edits apply without restart
    if key in dotenv_map:
        return dotenv_map[key]
    if cur_env is not None:
        # When an isolated AIVA_ENV_FILE is active, never leak unset keys from the workspace .env via load_dotenv()
        if os.environ.get("AIVA_ENV_FILE", "").strip() and cur_env == dotenv_startup_val and key not in _PROCESS_START_ENVIRON:
            return default
        return cur_env
    return default


def _as_bool(raw: str, default: bool = False) -> bool:
    if not raw:
        return default
    return raw.strip().lower() in ("true", "1", "yes", "on")


def _as_int(raw: str, default: int, minimum: Optional[int] = None) -> int:
    try:
        val = int(str(raw).strip())
    except Exception:
        val = default
    if minimum is not None and val < minimum:
        return minimum
    return val


@dataclass(frozen=True)
class WhatsAppConfig:
    whatsapp_enabled: bool
    aiva_whatsapp_number: str
    admin_email: str
    bridge_port: int
    bridge_secret: str
    backend_url: str
    access_username: str
    access_password: str
    access_password_hash: str
    access_epoch: int
    access_max_attempts: int
    access_lock_minutes: int
    access_challenge_ttl_minutes: int
    access_max_challenges_per_hour: int
    require_owner_approval: bool
    verify_mode: str
    max_verify_sends_per_day: int
    user_daily_message_cap: int
    user_can_send_whatsapp: bool
    allow_power_control: bool
    start_trigger: str
    stop_trigger: str
    session_idle_minutes: int
    reply_window_minutes: int
    max_outbound_per_hour: int
    max_message_chars: int
    reply_delay_ms: int
    quiet_hours_start: str
    quiet_hours_end: str
    unknown_sender_user_email: str
    inbox_retention_days: int
    mark_read_on_read: bool

    def __repr__(self) -> str:
        return (
            f"WhatsAppConfig(enabled={self.whatsapp_enabled}, "
            f"number={mask_phone(self.aiva_whatsapp_number)}, "
            f"epoch={self.access_epoch}, verify_mode={self.verify_mode!r})"
        )


def get_wa_config(*, env_path: Optional[Path] = None, force: bool = False) -> WhatsAppConfig:
    """Return hot-reloaded WhatsApp and access-control configuration (mtime-cached)."""
    dotenv_map = _load_dotenv_cached(env_path=env_path, force=force)
    mode = _resolve_key("WHATSAPP_VERIFY_MODE", dotenv_map, "inbound").strip().lower()
    if mode not in ("inbound", "outbound"):
        mode = "inbound"

    return WhatsAppConfig(
        whatsapp_enabled=_as_bool(_resolve_key("WHATSAPP_ENABLED", dotenv_map, "true"), True),
        aiva_whatsapp_number=normalize_phone(_resolve_key("AIVA_WHATSAPP_NUMBER", dotenv_map, "")),
        admin_email=_resolve_key("AIVA_ADMIN_EMAIL", dotenv_map, "").strip().lower(),
        bridge_port=_as_int(_resolve_key("WHATSAPP_BRIDGE_PORT", dotenv_map, "3001"), 3001, 1),
        bridge_secret=_resolve_key("WHATSAPP_BRIDGE_SECRET", dotenv_map, "aiva_default_local_bridge_secret_change_me"),
        backend_url=_resolve_key("WHATSAPP_BACKEND_URL", dotenv_map, "http://127.0.0.1:5000").strip(),
        access_username=_resolve_key("AIVA_ACCESS_USERNAME", dotenv_map, "").strip(),
        access_password=_resolve_key("AIVA_ACCESS_PASSWORD", dotenv_map, "").strip(),
        access_password_hash=_resolve_key("AIVA_ACCESS_PASSWORD_HASH", dotenv_map, "").strip(),
        access_epoch=_as_int(_resolve_key("AIVA_ACCESS_EPOCH", dotenv_map, "1"), 1, 1),
        access_max_attempts=_as_int(_resolve_key("AIVA_ACCESS_MAX_ATTEMPTS", dotenv_map, "3"), 3, 1),
        access_lock_minutes=_as_int(_resolve_key("AIVA_ACCESS_LOCK_MINUTES", dotenv_map, "30"), 30, 1),
        access_challenge_ttl_minutes=_as_int(_resolve_key("AIVA_ACCESS_CHALLENGE_TTL_MINUTES", dotenv_map, "5"), 5, 1),
        access_max_challenges_per_hour=_as_int(_resolve_key("AIVA_ACCESS_MAX_CHALLENGES_PER_HOUR", dotenv_map, "20"), 20, 1),
        require_owner_approval=_as_bool(_resolve_key("AIVA_REQUIRE_OWNER_APPROVAL", dotenv_map, "false"), False),
        verify_mode=mode,
        max_verify_sends_per_day=_as_int(_resolve_key("WHATSAPP_MAX_VERIFY_SENDS_PER_DAY", dotenv_map, "3"), 3, 1),
        user_daily_message_cap=_as_int(_resolve_key("AIVA_USER_DAILY_MESSAGE_CAP", dotenv_map, "100"), 100, 1),
        user_can_send_whatsapp=_as_bool(_resolve_key("AIVA_USER_CAN_SEND_WHATSAPP", dotenv_map, "false"), False),
        allow_power_control=_as_bool(_resolve_key("WHATSAPP_ALLOW_POWER_CONTROL", dotenv_map, "false"), False),
        start_trigger=_resolve_key("WHATSAPP_START_TRIGGER", dotenv_map, "aiva\\").strip() or "aiva\\",
        stop_trigger=_resolve_key("WHATSAPP_STOP_TRIGGER", dotenv_map, "stop\\").strip() or "stop\\",
        session_idle_minutes=_as_int(_resolve_key("WHATSAPP_SESSION_IDLE_MINUTES", dotenv_map, "60"), 60, 0),
        reply_window_minutes=_as_int(_resolve_key("WHATSAPP_REPLY_WINDOW_MINUTES", dotenv_map, "10"), 10, 1),
        max_outbound_per_hour=_as_int(_resolve_key("WHATSAPP_MAX_OUTBOUND_PER_HOUR", dotenv_map, "20"), 20, 1),
        max_message_chars=_as_int(_resolve_key("WHATSAPP_MAX_MESSAGE_CHARS", dotenv_map, "1500"), 1500, 100),
        reply_delay_ms=_as_int(_resolve_key("WHATSAPP_REPLY_DELAY_MS", dotenv_map, "700"), 700, 0),
        quiet_hours_start=_resolve_key("WHATSAPP_QUIET_HOURS_START", dotenv_map, "23:00").strip() or "23:00",
        quiet_hours_end=_resolve_key("WHATSAPP_QUIET_HOURS_END", dotenv_map, "07:00").strip() or "07:00",
        unknown_sender_user_email=_resolve_key("WHATSAPP_UNKNOWN_SENDER_USER_EMAIL", dotenv_map, "").strip().lower(),
        inbox_retention_days=_as_int(_resolve_key("WHATSAPP_INBOX_RETENTION_DAYS", dotenv_map, "7"), 7, 1),
        mark_read_on_read=_as_bool(_resolve_key("WHATSAPP_MARK_READ_ON_READ", dotenv_map, "true"), True),
    )


def hash_access_password(password: str, iterations: int = 260000) -> str:
    """Create a salted PBKDF2-SHA256 hash string suitable for AIVA_ACCESS_PASSWORD_HASH."""
    clean = (password or "").strip()
    salt = secrets.token_hex(16)
    dk = hashlib.pbkdf2_hmac("sha256", clean.encode("utf-8"), salt.encode("utf-8"), iterations)
    return f"pbkdf2:sha256:{iterations}${salt}${dk.hex()}"


def _verify_password_hash(password: str, encoded_hash: str) -> bool:
    candidate = (password or "").strip()
    spec = (encoded_hash or "").strip()
    if not spec:
        return False

    # Format: pbkdf2:sha256[:iters]$salt$hex (werkzeug-compatible too)
    if spec.startswith("pbkdf2:sha256"):
        try:
            parts = spec.split("$")
            if len(parts) == 3:
                hdr, salt, digest_hex = parts
                hdr_parts = hdr.split(":")
                iters = int(hdr_parts[2]) if len(hdr_parts) >= 3 else 260000
                dk = hashlib.pbkdf2_hmac("sha256", candidate.encode("utf-8"), salt.encode("utf-8"), iters)
                return hmac.compare_digest(dk.hex(), digest_hex.strip().lower())
        except Exception:
            pass

    # Format: sha256$salt$hex
    if spec.startswith("sha256$"):
        parts = spec.split("$")
        if len(parts) == 3:
            salt, digest_hex = parts[1], parts[2]
            actual = hashlib.sha256((salt + candidate).encode("utf-8")).hexdigest()
            return hmac.compare_digest(actual, digest_hex.strip().lower())

    # Format: sha256:<hex>
    if spec.startswith("sha256:"):
        digest_hex = spec.split(":", 1)[1].strip().lower()
        actual = hashlib.sha256(candidate.encode("utf-8")).hexdigest()
        return hmac.compare_digest(actual, digest_hex)

    # Fallback: Werkzeug / scrypt / pbkdf2 hashes if installed
    try:
        from werkzeug.security import check_password_hash
        return bool(check_password_hash(spec, candidate))
    except Exception:
        return False


def verify_access_credentials(username: str, password: str, cfg: Optional[WhatsAppConfig] = None) -> bool:
    """
    Constant-time verification of WhatsApp access username + password.
    Username: outer-trimmed and case-insensitive.
    Password: outer-trimmed only (internal spaces & case preserved).
    AIVA_ACCESS_PASSWORD_HASH takes precedence over AIVA_ACCESS_PASSWORD when set.
    Evaluates both fields before returning to prevent field timing leaks.
    """
    conf = cfg or get_wa_config()
    expected_user = (conf.access_username or "").strip().lower()
    typed_user = (username or "").strip().lower()
    typed_pwd = (password or "").strip()

    has_expected_pwd = bool(conf.access_password_hash or conf.access_password)
    if not expected_user or not has_expected_pwd:
        # Constant-time dummy comparison when unconfigured
        hmac.compare_digest(b"unconfigured_u", typed_user.encode("utf-8"))
        hmac.compare_digest(b"unconfigured_p", typed_pwd.encode("utf-8"))
        return False

    user_ok = hmac.compare_digest(expected_user.encode("utf-8"), typed_user.encode("utf-8"))

    if conf.access_password_hash:
        pwd_ok = _verify_password_hash(typed_pwd, conf.access_password_hash)
    else:
        expected_pwd_hash = hashlib.sha256(conf.access_password.encode("utf-8")).digest()
        typed_pwd_hash = hashlib.sha256(typed_pwd.encode("utf-8")).digest()
        pwd_ok = hmac.compare_digest(expected_pwd_hash, typed_pwd_hash)

    return bool(user_ok and pwd_ok)


def is_admin_email(email: Optional[str], cfg: Optional[WhatsAppConfig] = None) -> bool:
    conf = cfg or get_wa_config()
    admin = (conf.admin_email or "").strip().lower()
    if not admin or not email:
        return False
    return hmac.compare_digest(admin.encode("utf-8"), email.strip().lower().encode("utf-8"))


def is_admin_user(user: Optional[Dict[str, Any]], cfg: Optional[WhatsAppConfig] = None) -> bool:
    if not user or not isinstance(user, dict):
        return False
    return is_admin_email(user.get("email"), cfg=cfg)


def get_trigger_stem(trigger: str, default_stem: str = "aiva") -> str:
    raw = (trigger or "").strip()
    while raw.endswith("\\") or raw.endswith("/"):
        raw = raw[:-1].strip()
    return (raw or default_stem).lower()


def match_start_trigger(text: str, start_trigger: Optional[str] = None) -> Tuple[bool, str]:
    """
    Check whether text matches the configured start trigger ('aiva\\' or 'aiva/', case-insensitive, trimmed).
    Bare 'aiva' without trailing '\\' or '/' never activates.
    Returns (matched, inline_command).
    """
    if not text:
        return False, ""
    trig = start_trigger if start_trigger is not None else get_wa_config().start_trigger
    stem = get_trigger_stem(trig, "aiva")
    trimmed = text.strip()
    m = re.match(rf"^{re.escape(stem)}[\\/]([\s\S]*)$", trimmed, re.IGNORECASE)
    if not m:
        return False, ""
    inline = (m.group(1) or "").strip()
    return True, inline


def match_stop_trigger(text: str, stop_trigger: Optional[str] = None) -> bool:
    """Check whether text matches the configured stop trigger ('stop\\' or 'stop/', case-insensitive, trimmed)."""
    if not text:
        return False
    trig = stop_trigger if stop_trigger is not None else get_wa_config().stop_trigger
    stem = get_trigger_stem(trig, "stop")
    trimmed = text.strip()
    return bool(re.match(rf"^{re.escape(stem)}[\\/]$", trimmed, re.IGNORECASE))


def validate_preferred_display_name(raw_name: str) -> Tuple[bool, str]:
    """Validate explicit WhatsApp Entry-A user display name (never LLM-extracted)."""
    clean = " ".join((raw_name or "").strip().split())
    if not clean or len(clean) < 1 or len(clean) > 40:
        return False, ""
    if any(ord(c) < 32 for c in clean):
        return False, ""
    # Allow Unicode letters/digits plus safe spaces, hyphens, apostrophes, periods
    if not re.match(r"^[\w\s.\-'\u0C00-\u0C7F]+$", clean, re.UNICODE):
        return False, ""
    # Reject control/injection punctuation or pure non-alnum
    if not any(ch.isalnum() for ch in clean):
        return False, ""
    return True, clean
