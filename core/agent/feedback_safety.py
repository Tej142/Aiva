"""Privacy, phone masking, and secret redaction for agent feedback exchanges."""

import re
from typing import Optional, Tuple

from core.memory.safety import (
    contains_sensitive_data,
    _API_KEY_PATTERNS,
    _PASSWORD_PATTERNS,
    _OTP_PATTERNS,
    _GOV_ID_PATTERNS,
    _CARD_NUMBER_RE,
    _CARD_CONTEXT_RE,
)

# Matches international numbers (+12345678901, +91 98765 43210) or standalone 10-12 digit numbers
_PHONE_PATTERN = re.compile(
    r"(?:\+?\d{1,3}[-.\s]?)?\(?\d{2,4}\)?[-.\s]?\d{3,4}[-.\s]?\d{3,4}\b|\b\d{10,12}\b"
)


def mask_phone_numbers_in_text(text: Optional[str]) -> str:
    """Mask phone numbers inside free text, retaining at most the last 4 digits."""
    if not text:
        return ""

    def _replace_phone(m: re.Match) -> str:
        raw = m.group(0)
        digits = re.sub(r"\D", "", raw)
        if len(digits) >= 7:
            last4 = digits[-4:]
            masked_prefix = "*" * (len(digits) - 4)
            return f"+{masked_prefix}{last4}"
        return raw

    return _PHONE_PATTERN.sub(_replace_phone, str(text))


def redact_secrets_in_text(text: Optional[str]) -> str:
    """Redact sensitive credentials, OTPs, API keys, passwords, cards, and govt IDs."""
    if not text:
        return ""
    cleaned = str(text)

    # 1. API Keys
    for pat in _API_KEY_PATTERNS:
        cleaned = pat.sub("[REDACTED_API_KEY]", cleaned)

    # 2. Passwords / PINs
    for pat in _PASSWORD_PATTERNS:
        cleaned = pat.sub("[REDACTED_PASSWORD]", cleaned)

    # 3. OTPs / Verification codes
    for pat in _OTP_PATTERNS:
        cleaned = pat.sub("[REDACTED_OTP]", cleaned)

    # 4. Government IDs / SSNs / Aadhaar / PAN
    for pat in _GOV_ID_PATTERNS:
        cleaned = pat.sub("[REDACTED_GOVT_ID]", cleaned)

    # 5. Payment Cards
    for m in _CARD_NUMBER_RE.finditer(cleaned):
        digits = re.sub(r"\D", "", m.group(0))
        if 13 <= len(digits) <= 19:
            cleaned = cleaned.replace(m.group(0), "[REDACTED_PAYMENT_CARD]")

    # Double check if any sensitive token persists
    is_sensitive, _ = contains_sensitive_data(cleaned)
    if is_sensitive:
        cleaned = "[REDACTED_SENSITIVE_DATA]"

    return cleaned


def sanitize_feedback_text(text: Optional[str]) -> str:
    """Combined sanitizer: masks phone numbers and redacts all secrets."""
    if not text:
        return ""
    # Mask phone numbers first
    with_masked_phones = mask_phone_numbers_in_text(text)
    # Redact credentials and sensitive data
    return redact_secrets_in_text(with_masked_phones)

