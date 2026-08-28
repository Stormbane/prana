"""Web reach for the voice — search + fetch, fail-closed (B4).

The realtime model never browses. Our code runs the search (Brave API
behind a one-function seam — SearXNG could replace it without touching
tools), filters and truncates, and only the text we return reaches the
model provider.

`fetch` implements the full SSRF contract from cross-review #4 (a
name-based denylist was bypassable via IPv6, encodings, redirects and
DNS rebinding):

- http(s) only, ports 80/443 only, no credentials in the URL;
- the hostname is resolved and EVERY answer must be a global unicast
  address (loopback, RFC1918, link-local, ULA, CGNAT, metadata,
  multicast, reserved all refuse) — alternate numeric encodings are
  inert because validation happens on RESOLVED addresses;
- the connection is PINNED to the validated address (Host/SNI keep the
  name), so rebinding between check and connect changes nothing;
- every redirect hop re-validates under the same contract, max 3;
- byte and time caps; extracted text capped again.

No key / no network / refused target -> a spoken-honest error string,
never an exception loop.
"""

from __future__ import annotations

import http.client
import ipaddress
import json
import logging
import re
import socket
import ssl
import urllib.parse
from pathlib import Path
from typing import Callable, Optional

logger = logging.getLogger(__name__)

BRAVE_KEY_FILE = Path.home() / ".narada" / ".brave-search.key"
BRAVE_ENDPOINT = "api.search.brave.com"

MAX_RESULTS = 5
MAX_SNIPPET_CHARS = 280
MAX_REDIRECTS = 3
MAX_RAW_BYTES = 200_000
MAX_TEXT_CHARS = 4_000
TIMEOUT_S = 8.0

ALLOWED_PORTS = {80, 443}


class WebRefused(RuntimeError):
    """The target failed the safety contract. The reason is speakable."""


class WebUnavailable(RuntimeError):
    """Backend not configured / network trouble. Speakable."""


# ── validation ───────────────────────────────────────────────────────

def _addr_ok(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """Global unicast only. is_global already excludes loopback,
    private, link-local, ULA, CGNAT (100.64/10), reserved and
    documentation ranges; the explicit checks are belt-and-braces."""
    return (ip.is_global
            and not ip.is_multicast
            and not ip.is_loopback
            and not ip.is_link_local
            and not ip.is_private
            and not ip.is_unspecified
            and not ip.is_reserved)


def validate_url(
    url: str,
    resolver: Callable = socket.getaddrinfo,
) -> tuple[str, str, int, str, str]:
    """Returns (scheme, host, port, pinned_ip, path_query) or raises
    WebRefused. EVERY resolved address must pass; the first one becomes
    the pinned connect target."""
    try:
        parts = urllib.parse.urlsplit(url)
    except ValueError as exc:
        raise WebRefused(f"unparseable url: {exc}") from None
    if parts.scheme not in ("http", "https"):
        raise WebRefused("only http(s) urls")
    if parts.username is not None or parts.password is not None:
        raise WebRefused("credentials in urls are refused")
    host = parts.hostname
    if not host:
        raise WebRefused("no host")
    port = parts.port or (443 if parts.scheme == "https" else 80)
    if port not in ALLOWED_PORTS:
        raise WebRefused(f"port {port} is refused")

    try:
        infos = resolver(host, port, proto=socket.IPPROTO_TCP)
    except (socket.gaierror, OSError) as exc:
        raise WebRefused(f"cannot resolve {host}: {exc}") from None
    if not infos:
        raise WebRefused(f"no addresses for {host}")

    ips = []
    for info in infos:
        addr = info[4][0]
        try:
            ip = ipaddress.ip_address(addr.split("%", 1)[0])
        except ValueError:
            raise WebRefused(f"bad address {addr!r}") from None
        mapped = getattr(ip, "ipv4_mapped", None)
        if mapped is not None:
            ip = mapped
        if not _addr_ok(ip):
            raise WebRefused(
                f"{host} resolves into a non-public network — refused")
        ips.append(addr)

    path = parts.path or "/"
    if parts.query:
        path += "?" + parts.query
    return parts.scheme, host, port, ips[0], path


# ── pinned connections ───────────────────────────────────────────────

class _PinnedHTTPConnection(http.client.HTTPConnection):
    def __init__(self, host: str, pinned_ip: str, port: int):
        super().__init__(host, port, timeout=TIMEOUT_S)
        self._pinned_ip = pinned_ip

    def connect(self):
        self.sock = socket.create_connection(
            (self._pinned_ip, self.port), self.timeout)


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, host: str, pinned_ip: str, port: int):
        super().__init__(host, port, timeout=TIMEOUT_S)
        self._pinned_ip = pinned_ip

    def connect(self):
        raw = socket.create_connection(
            (self._pinned_ip, self.port), self.timeout)
        ctx = ssl.create_default_context()
        # SNI + certificate verification against the NAME, while the
        # TCP connection goes to the pinned address.
        self.sock = ctx.wrap_socket(raw, server_hostname=self.host)


def _open_pinned(scheme: str, host: str, ip: str, port: int):
    cls = _PinnedHTTPSConnection if scheme == "https" else _PinnedHTTPConnection
    return cls(host, ip, port)


# ── fetch ────────────────────────────────────────────────────────────

_TAG_STRIP = re.compile(
    r"<(script|style)\b[^>]*>.*?</\1>", re.S | re.I)
_TAGS = re.compile(r"<[^>]+>")
_WS = re.compile(r"[ \t\r\f\v]+")


def _extract_text(html: str) -> str:
    text = _TAG_STRIP.sub(" ", html)
    text = _TAGS.sub(" ", text)
    text = urllib.parse.unquote(text)
    import html as _html
    text = _html.unescape(text)
    text = _WS.sub(" ", text)
    text = re.sub(r"\n\s*\n+", "\n\n", text)
    return text.strip()[:MAX_TEXT_CHARS]


def fetch(
    url: str,
    resolver: Callable = socket.getaddrinfo,
    opener: Callable = _open_pinned,
) -> str:
    """Fetch a public page, return extracted text. Raises WebRefused /
    WebUnavailable with speakable reasons."""
    current = url
    for _hop in range(MAX_REDIRECTS + 1):
        scheme, host, port, ip, path = validate_url(current, resolver)
        conn = opener(scheme, host, ip, port)
        try:
            conn.request("GET", path, headers={
                "Host": host,
                "User-Agent": "narada-voice/1.0 (+personal assistant)",
                "Accept": "text/html,text/plain,application/xhtml+xml",
            })
            resp = conn.getresponse()
            if 300 <= resp.status < 400:
                loc = resp.getheader("Location")
                if not loc:
                    raise WebUnavailable(f"redirect without target "
                                         f"({resp.status})")
                # Every hop re-enters the full contract.
                current = urllib.parse.urljoin(current, loc)
                continue
            if resp.status != 200:
                raise WebUnavailable(f"page returned {resp.status}")
            raw = resp.read(MAX_RAW_BYTES)
            body = raw.decode("utf-8", errors="replace")
            return _extract_text(body)
        except (OSError, http.client.HTTPException, ssl.SSLError) as exc:
            raise WebUnavailable(
                f"couldn't reach {host}: {type(exc).__name__}") from None
        finally:
            conn.close()
    raise WebRefused("too many redirects")


# ── search (Brave behind a one-function seam) ────────────────────────

def _load_brave_key() -> Optional[str]:
    try:
        key = BRAVE_KEY_FILE.read_text(encoding="utf-8").strip()
        return key or None
    except FileNotFoundError:
        return None
    except OSError as exc:
        logger.warning("brave key unreadable: %s", exc)
        return None


def _brave_search(query: str, key: str) -> list[dict]:
    conn = http.client.HTTPSConnection(BRAVE_ENDPOINT, timeout=TIMEOUT_S)
    try:
        q = urllib.parse.quote(query)
        conn.request(
            "GET", f"/res/v1/web/search?q={q}&count={MAX_RESULTS}",
            headers={"Accept": "application/json",
                     "X-Subscription-Token": key})
        resp = conn.getresponse()
        if resp.status == 429:
            raise WebUnavailable("search quota exhausted for now")
        if resp.status != 200:
            raise WebUnavailable(f"search backend returned {resp.status}")
        payload = json.loads(resp.read(1_000_000).decode("utf-8", "replace"))
    except (OSError, http.client.HTTPException, json.JSONDecodeError) as exc:
        raise WebUnavailable(
            f"search backend unreachable: {type(exc).__name__}") from None
    finally:
        conn.close()
    out = []
    for item in (payload.get("web", {}).get("results", []) or [])[:MAX_RESULTS]:
        out.append({
            "title": str(item.get("title", ""))[:120],
            "url": str(item.get("url", ""))[:300],
            "snippet": str(item.get("description", ""))[:MAX_SNIPPET_CHARS],
        })
    return out


def search(query: str, backend: Optional[Callable] = None) -> list[dict]:
    """Search the web. Fail-closed: no key -> WebUnavailable with a
    speakable message."""
    query = (query or "").strip()
    if not query:
        raise WebUnavailable("empty search")
    if backend is not None:
        return backend(query)
    key = _load_brave_key()
    if key is None:
        raise WebUnavailable(
            "web search isn't set up yet — the Brave API key is missing")
    return _brave_search(query, key)
