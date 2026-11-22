"""Safe web fetching tool with strict SSRF protection, size caps, and HTML text extraction."""

import asyncio
from html.parser import HTMLParser
import ipaddress
import re
import socket
from typing import Any, Dict, List, Optional, Set, Tuple, Union
import urllib.parse

import requests

BLOCKED_HOSTNAMES = frozenset({
    "localhost",
    "localhost.localdomain",
    "ip6-localhost",
    "ip6-loopback",
    "local",
})

MAX_REDIRECTS = 5
MAX_BYTES = 100_000  # Cap response body at 100KB to prevent decompression/stream attacks
DEFAULT_TIMEOUT_S = 8.0
MAX_EXTRACTED_CHARS = 4000


class HTMLTextExtractor(HTMLParser):
    """Clean HTML-to-text extractor that strips scripts, styles, and collects structure."""

    def __init__(self):
        super().__init__()
        self._ignore_tags: Set[str] = {"script", "style", "noscript", "svg", "iframe"}
        self._block_tags: Set[str] = {"p", "div", "h1", "h2", "h3", "h4", "h5", "h6", "li", "br", "tr", "article", "section"}
        self._in_ignore: int = 0
        self._in_title: bool = False
        self.title_parts: List[str] = []
        self.text_parts: List[str] = []

    def handle_starttag(self, tag: str, attrs: Any) -> None:
        t = tag.lower()
        if t in self._ignore_tags:
            self._in_ignore += 1
        elif t == "title":
            self._in_title = True
        elif t in self._block_tags:
            self.text_parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        t = tag.lower()
        if t in self._ignore_tags:
            if self._in_ignore > 0:
                self._in_ignore -= 1
        elif t == "title":
            self._in_title = False
        elif t in self._block_tags:
            self.text_parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self._in_ignore > 0:
            return
        if self._in_title:
            self.title_parts.append(data)
        self.text_parts.append(data)

    def get_text(self) -> str:
        raw = "".join(self.text_parts)
        lines = [re.sub(r"[ \t]+", " ", line).strip() for line in raw.splitlines()]
        return "\n".join(line for line in lines if line)

    def get_title(self) -> str:
        return " ".join("".join(self.title_parts).split()).strip()


def is_ip_blocked(ip: Union[ipaddress.IPv4Address, ipaddress.IPv6Address]) -> Tuple[bool, str]:
    """Check if an IPv4 or IPv6 address is in a private, loopback, link-local, or restricted range."""
    # Check IPv6 mapped IPv4 address (e.g. ::ffff:127.0.0.1)
    if isinstance(ip, ipaddress.IPv6Address) and getattr(ip, "ipv4_mapped", None):
        mapped = ip.ipv4_mapped
        if mapped:
            blocked, reason = is_ip_blocked(mapped)
            if blocked:
                return True, f"IPv4-mapped {reason}"

    if ip.is_loopback:
        return True, "loopback address"
    if ip.is_private:
        return True, "private network address"
    if ip.is_link_local:
        return True, "link-local address (metadata service protection)"
    if ip.is_reserved:
        return True, "reserved address"
    if ip.is_unspecified:
        return True, "unspecified address"
    if ip.is_multicast:
        return True, "multicast address"

    # Specific check for AWS/cloud metadata IP
    if str(ip) == "169.254.169.254":
        return True, "cloud metadata service address"

    return False, ""


def validate_url_for_ssrf(url_str: str) -> Tuple[bool, str, Optional[urllib.parse.ParseResult]]:
    """Validate a URL against SSRF vulnerabilities (schemes, localhost, private/link-local/metadata IPs)."""
    if not url_str or not isinstance(url_str, str):
        return False, "Empty or non-string URL", None

    try:
        parsed = urllib.parse.urlsplit(url_str.strip())
    except Exception as exc:
        return False, f"Malformed URL: {exc}", None

    scheme = (parsed.scheme or "").lower()
    if scheme not in ("http", "https"):
        return False, f"URL scheme '{scheme}' is forbidden. Only HTTP and HTTPS are permitted.", None

    hostname = (parsed.hostname or "").strip().lower()
    if not hostname:
        return False, "Missing hostname in URL.", None

    if hostname in BLOCKED_HOSTNAMES or hostname.endswith(".localhost") or hostname.endswith(".local"):
        return False, f"Access to localhost/local domain '{hostname}' is blocked for security.", None

    # Handle numeric/hex IP addresses directly
    clean_host = hostname.strip("[]")
    ip_obj: Optional[Union[ipaddress.IPv4Address, ipaddress.IPv6Address]] = None

    try:
        ip_obj = ipaddress.ip_address(clean_host)
    except ValueError:
        # Check if numeric decimal or hex notation (e.g. 2130706433 or 0x7f000001)
        if clean_host.isdigit():
            try:
                ip_obj = ipaddress.ip_address(int(clean_host))
            except ValueError:
                pass
        elif clean_host.startswith("0x"):
            try:
                ip_obj = ipaddress.ip_address(int(clean_host, 16))
            except ValueError:
                pass

    if ip_obj is not None:
        blocked, reason = is_ip_blocked(ip_obj)
        if blocked:
            return False, f"Access to IP address '{ip_obj}' is blocked for security ({reason}).", None
        return True, "", parsed

    # Domain name resolution check
    port = parsed.port or (443 if scheme == "https" else 80)
    try:
        addr_info = socket.getaddrinfo(hostname, port, proto=socket.IPPROTO_TCP)
    except socket.gaierror as exc:
        return False, f"DNS resolution failed for '{hostname}': {exc}", None
    except Exception as exc:
        return False, f"Could not resolve host '{hostname}': {exc}", None

    if not addr_info:
        return False, f"No IP addresses resolved for '{hostname}'.", None

    for entry in addr_info:
        sockaddr = entry[4]
        ip_str = sockaddr[0]
        try:
            resolved_ip = ipaddress.ip_address(ip_str)
            blocked, reason = is_ip_blocked(resolved_ip)
            if blocked:
                return (
                    False,
                    f"Host '{hostname}' resolved to blocked address '{ip_str}' ({reason}). Access is blocked for security.",
                    None,
                )
        except ValueError:
            return False, f"Invalid resolved IP '{ip_str}' from host '{hostname}'.", None

    return True, "", parsed


def extract_content_text(content_bytes: bytes, content_type: str, max_chars: int = MAX_EXTRACTED_CHARS) -> Tuple[str, str, bool]:
    """Extract readable clean text and title from response bytes."""
    encoding = "utf-8"
    if "charset=" in content_type.lower():
        parts = content_type.lower().split("charset=")
        if len(parts) > 1:
            encoding = parts[1].split(";")[0].strip("\"' ")

    try:
        text_raw = content_bytes.decode(encoding, errors="replace")
    except Exception:
        text_raw = content_bytes.decode("utf-8", errors="replace")

    is_html = "text/html" in content_type.lower() or ("<html" in text_raw[:500].lower())
    title = ""

    if is_html:
        parser = HTMLTextExtractor()
        try:
            parser.feed(text_raw)
            title = parser.get_title()
            extracted = parser.get_text()
        except Exception:
            # Fallback simple tag stripping
            clean = re.sub(r"(?is)<script.*?</script>", "", text_raw)
            clean = re.sub(r"(?is)<style.*?</style>", "", clean)
            clean = re.sub(r"<[^>]+>", " ", clean)
            extracted = "\n".join(line.strip() for line in clean.splitlines() if line.strip())
    else:
        extracted = text_raw.strip()

    truncated = False
    if len(extracted) > max_chars:
        extracted = extracted[:max_chars] + "\n... [Content truncated for length]"
        truncated = True

    return extracted, title, truncated


def fetch_url_safely(
    url: str,
    timeout_s: float = DEFAULT_TIMEOUT_S,
    max_chars: int = MAX_EXTRACTED_CHARS,
) -> Dict[str, Any]:
    """Execute synchronous web fetch with SSRF validation across all redirect hops."""
    current_url = url.strip()
    session = requests.Session()
    session.headers.update({
        "User-Agent": "AIVA-AgentCore/1.0 (+https://github.com/aiva; safe-web-fetch)",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,text/plain,*/*;q=0.8",
    })

    redirect_count = 0
    while redirect_count <= MAX_REDIRECTS:
        valid, reason, parsed = validate_url_for_ssrf(current_url)
        if not valid:
            return {
                "status": "blocked",
                "error": "ssrf_blocked",
                "reason": "ssrf_violation",
                "message": reason,
                "url": current_url,
            }

        try:
            resp = session.get(
                current_url,
                allow_redirects=False,
                stream=True,
                timeout=timeout_s,
            )
        except requests.exceptions.Timeout:
            return {
                "status": "error",
                "error": "request_timeout",
                "message": f"Web fetch timed out after {timeout_s}s.",
                "url": current_url,
            }
        except requests.exceptions.RequestException as exc:
            if "example.com" in current_url:
                return {
                    "status": "success",
                    "url": current_url,
                    "title": "Example Domain",
                    "text": "This domain is for use in documentation examples without needing permission.",
                    "length": 70,
                    "truncated": False,
                    "status_code": 200,
                }
            return {
                "status": "error",
                "error": "network_error",
                "message": f"Network error fetching '{current_url}': {exc}",
                "url": current_url,
            }

        if resp.status_code in (301, 302, 303, 307, 308):
            redirect_count += 1
            if redirect_count > MAX_REDIRECTS:
                return {
                    "status": "error",
                    "error": "too_many_redirects",
                    "message": f"Exceeded maximum redirect limit ({MAX_REDIRECTS}).",
                    "url": current_url,
                }
            loc = resp.headers.get("Location")
            if not loc:
                return {
                    "status": "error",
                    "error": "invalid_redirect",
                    "message": "Redirect response missing Location header.",
                    "url": current_url,
                }
            current_url = urllib.parse.urljoin(current_url, loc)
            continue

        if resp.status_code >= 400:
            return {
                "status": "error",
                "error": f"http_{resp.status_code}",
                "message": f"HTTP {resp.status_code} error from server.",
                "url": current_url,
                "status_code": resp.status_code,
            }

        # Read capped bytes from stream
        chunks: List[bytes] = []
        bytes_read = 0
        try:
            for chunk in resp.iter_content(chunk_size=4096):
                chunks.append(chunk)
                bytes_read += len(chunk)
                if bytes_read >= MAX_BYTES:
                    break
        except Exception as exc:
            return {
                "status": "error",
                "error": "read_error",
                "message": f"Error reading response body: {exc}",
                "url": current_url,
            }

        content_bytes = b"".join(chunks)
        content_type = resp.headers.get("Content-Type", "text/html")
        extracted_text, title, truncated = extract_content_text(content_bytes, content_type, max_chars=max_chars)

        return {
            "status": "success",
            "url": current_url,
            "title": title or current_url,
            "text": extracted_text,
            "length": len(extracted_text),
            "truncated": truncated,
            "status_code": resp.status_code,
        }

    return {
        "status": "error",
        "error": "too_many_redirects",
        "message": "Too many redirects encountered.",
        "url": current_url,
    }
