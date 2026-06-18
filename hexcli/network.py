#!/usr/bin/env python3
"""hexcli.network — Online detection and URL fetching for the fetch_url tool."""
from __future__ import annotations

import html.parser
import ipaddress
import re
import socket
import time
import urllib.error
import urllib.parse
import urllib.request

# ---------------------------------------------------------------------------
# Online probe — cached 30 s
# ---------------------------------------------------------------------------

_online_result: bool | None = None
_online_ts: float = 0.0
_ONLINE_TTL = 30.0
_PROBE_HOST = "8.8.8.8"
_PROBE_PORT = 53
_PROBE_TIMEOUT = 0.2  # 200 ms


def is_online() -> bool:
    """Return True if the internet appears reachable. Result is cached for 30 s."""
    global _online_result, _online_ts
    now = time.monotonic()
    if _online_result is not None and now - _online_ts < _ONLINE_TTL:
        return _online_result
    try:
        sock = socket.create_connection((_PROBE_HOST, _PROBE_PORT), timeout=_PROBE_TIMEOUT)
        sock.close()
        result = True
    except OSError:
        result = False
    _online_result = result
    _online_ts = now
    return result


# ---------------------------------------------------------------------------
# URL security check
# ---------------------------------------------------------------------------

def _is_blocked_url(url: str) -> str | None:
    """Return an error string if the URL should be blocked, None if safe to fetch."""
    try:
        parsed = urllib.parse.urlsplit(url)
    except Exception:
        return "fetch_url: invalid URL."

    scheme = (parsed.scheme or "").lower()
    if scheme not in ("http", "https"):
        return f"fetch_url: only http/https URLs are allowed (got scheme {scheme!r})."

    host = (parsed.hostname or "").lower()
    if not host:
        return "fetch_url: URL has no host."
    if host == "localhost":
        return "fetch_url: private/local addresses are blocked."

    try:
        addr = ipaddress.ip_address(host)
        if addr.is_private or addr.is_loopback or addr.is_link_local:
            return f"fetch_url: private address {host!r} is blocked."
    except ValueError:
        pass  # hostname — let DNS resolve it; private hostnames can't be easily checked here

    return None


# ---------------------------------------------------------------------------
# HTML stripping
# ---------------------------------------------------------------------------

class _StripHTML(html.parser.HTMLParser):
    _SKIP = frozenset({"script", "style", "nav", "header", "footer", "aside"})
    _BLOCK = frozenset({"p", "br", "div", "li", "h1", "h2", "h3", "h4", "h5", "h6", "tr", "td", "section", "article"})

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._parts: list[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag: str, attrs: object) -> None:
        if tag in self._SKIP:
            self._skip_depth += 1

    def handle_endtag(self, tag: str) -> None:
        if tag in self._SKIP:
            self._skip_depth = max(0, self._skip_depth - 1)
        elif tag in self._BLOCK and not self._skip_depth:
            self._parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self._skip_depth:
            self._parts.append(data)

    def get_text(self) -> str:
        text = "".join(self._parts)
        text = re.sub(r"\n{3,}", "\n\n", text)
        text = re.sub(r"[ \t]+", " ", text)
        return text.strip()


# ---------------------------------------------------------------------------
# fetch_url
# ---------------------------------------------------------------------------

def fetch_url(url: str, max_chars: int = 2500) -> str:
    """Fetch a URL and return plain text. Blocks private IPs and non-HTTP/HTTPS schemes."""
    block = _is_blocked_url(url)
    if block:
        return block

    if not is_online():
        return "fetch_url: no network connection available."

    try:
        req = urllib.request.Request(
            url,
            headers={"User-Agent": "hexcli/1.3 (local agent; +https://github.com/NathanL15/Hex-CLI)"},
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            content_type = resp.headers.get("Content-Type", "")
            raw = resp.read(max_chars * 8)
    except urllib.error.HTTPError as exc:
        return f"fetch_url: HTTP {exc.code} {exc.reason} — {url}"
    except urllib.error.URLError as exc:
        return f"fetch_url: could not reach {url!r}: {exc.reason}"
    except Exception as exc:
        return f"fetch_url: error — {exc}"

    try:
        text_raw = raw.decode("utf-8", errors="replace")
    except Exception:
        text_raw = raw.decode("latin-1", errors="replace")

    if "html" in content_type.lower() or text_raw.strip().lower().startswith("<!"):
        parser = _StripHTML()
        try:
            parser.feed(text_raw)
            text = parser.get_text()
        except Exception:
            text = text_raw
    else:
        text = text_raw

    if len(text) > max_chars:
        text = text[:max_chars] + f"\n...[truncated to {max_chars} chars]"

    return text or f"(empty response from {url})"
