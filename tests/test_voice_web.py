"""B4 — the SSRF contract, adversarially (cross-review #4 test matrix)."""

from __future__ import annotations

import socket

import pytest

from prana.voice import web
from prana.voice.web import WebRefused, WebUnavailable, fetch, validate_url


def resolver_for(*addrs, family=socket.AF_INET):
    def resolve(host, port, proto=None):
        return [(family, socket.SOCK_STREAM, 6, "", (a, port)) for a in addrs]
    return resolve


PUBLIC = "93.184.216.34"


# ── scheme / port / credentials ──────────────────────────────────────

def test_non_http_schemes_refused():
    for url in ("ftp://example.com/", "file:///etc/passwd",
                "gopher://example.com/"):
        with pytest.raises(WebRefused):
            validate_url(url, resolver_for(PUBLIC))


def test_odd_ports_refused():
    with pytest.raises(WebRefused):
        validate_url("http://example.com:8080/", resolver_for(PUBLIC))
    with pytest.raises(WebRefused):
        validate_url("https://example.com:6379/", resolver_for(PUBLIC))


def test_credentials_refused():
    with pytest.raises(WebRefused):
        validate_url("https://user:pass@example.com/", resolver_for(PUBLIC))


# ── address classes, v4 and v6, literal and resolved ─────────────────

BAD_LITERALS = [
    "http://127.0.0.1/", "http://10.0.0.5/", "http://192.168.86.20/",
    "http://172.16.0.1/", "http://169.254.169.254/latest/meta-data/",
    "http://100.64.0.1/", "http://0.0.0.0/",
    "http://[::1]/", "http://[fe80::1]/", "http://[fd41:86f2::1]/",
    "http://[::ffff:127.0.0.1]/", "http://[::ffff:10.0.0.1]/",
]


@pytest.mark.parametrize("url", BAD_LITERALS)
def test_private_literals_refused(url):
    """Literals resolve to themselves via the real resolver path — feed
    them through a passthrough resolver to keep tests offline."""
    host = url.split("//")[1].split("/")[0].strip("[]")
    fam = socket.AF_INET6 if ":" in host else socket.AF_INET
    with pytest.raises(WebRefused):
        validate_url(url, resolver_for(host, family=fam))


def test_encoded_addresses_caught_after_resolution():
    """Decimal/octal/hex encodings are inert because validation runs on
    what the resolver RETURNS: whatever 'that name' really is."""
    with pytest.raises(WebRefused):
        validate_url("http://2130706433/", resolver_for("127.0.0.1"))
    with pytest.raises(WebRefused):
        validate_url("http://0x7f.1/", resolver_for("127.0.0.1"))


def test_multi_answer_one_private_refuses_all():
    with pytest.raises(WebRefused):
        validate_url("http://evil.example/",
                     resolver_for(PUBLIC, "10.0.0.1"))


def test_public_host_passes_and_pins_first_address():
    scheme, host, port, ip, path = validate_url(
        "https://example.com/a?b=1", resolver_for(PUBLIC, "93.184.216.35"))
    assert (scheme, host, port) == ("https", "example.com", 443)
    assert ip == PUBLIC
    assert path == "/a?b=1"


# ── rebinding: the pin is the defense ────────────────────────────────

def test_rebinding_cannot_change_connect_target():
    """Resolver answers public on validation; the fetch connects to the
    PINNED address — a second resolution never happens."""
    calls = {"n": 0}

    def flapping_resolver(host, port, proto=None):
        calls["n"] += 1
        addr = PUBLIC if calls["n"] == 1 else "127.0.0.1"
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (addr, port))]

    dialed = []

    class FakeConn:
        def __init__(self, scheme, host, ip, port):
            dialed.append(ip)

        def request(self, *a, **k): pass

        def getresponse(self):
            class R:
                status = 200
                def read(self, n): return b"<html>hello grove</html>"
                def getheader(self, k): return None
            return R()

        def close(self): pass

    text = fetch("http://rebind.example/", resolver=flapping_resolver,
                 opener=lambda s, h, ip, p: FakeConn(s, h, ip, p))
    assert dialed == [PUBLIC]
    assert "hello grove" in text


# ── redirects: every hop re-validates ────────────────────────────────

def test_redirect_into_private_space_refused():
    hosts = {"public.example": PUBLIC, "internal.example": "192.168.1.1"}

    def resolver(host, port, proto=None):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "",
                 (hosts[host], port))]

    class RedirConn:
        def __init__(self, scheme, host, ip, port): pass
        def request(self, *a, **k): pass

        def getresponse(self):
            class R:
                status = 302
                def read(self, n): return b""
                def getheader(self, k):
                    return "http://internal.example/admin"
            return R()

        def close(self): pass

    with pytest.raises(WebRefused):
        fetch("http://public.example/", resolver=resolver,
              opener=lambda s, h, ip, p: RedirConn(s, h, ip, p))


def test_redirect_loop_bounded():
    class LoopConn:
        def __init__(self, scheme, host, ip, port): pass
        def request(self, *a, **k): pass

        def getresponse(self):
            class R:
                status = 301
                def read(self, n): return b""
                def getheader(self, k): return "http://public.example/"
            return R()

        def close(self): pass

    with pytest.raises(WebRefused, match="redirect"):
        fetch("http://public.example/", resolver=resolver_for(PUBLIC),
              opener=lambda s, h, ip, p: LoopConn(s, h, ip, p))


# ── caps + search fail-closed ────────────────────────────────────────

def test_text_extraction_caps_and_strips():
    class BigConn:
        def __init__(self, scheme, host, ip, port): pass
        def request(self, *a, **k): pass

        def getresponse(self):
            class R:
                status = 200
                def read(self, n):
                    body = ("<script>evil()</script><p>word </p>" * 5000)
                    return body.encode()[:n]
                def getheader(self, k): return None
            return R()

        def close(self): pass

    text = fetch("http://public.example/", resolver=resolver_for(PUBLIC),
                 opener=lambda s, h, ip, p: BigConn(s, h, ip, p))
    assert len(text) <= web.MAX_TEXT_CHARS
    assert "evil()" not in text


def test_search_without_key_uses_keyless_backend(monkeypatch):
    """No Brave key is not an outage: the keyless DDG path serves
    (2026-08-31 — search must never sit behind a paywall)."""
    monkeypatch.setattr(web, "_load_brave_key", lambda: None)
    monkeypatch.setattr(web, "_ddg_search",
                        lambda q: [{"title": "t", "url": "u", "snippet": "s"}])
    assert web.search("weather brisbane")[0]["title"] == "t"


def test_search_no_key_and_backend_down_fails_speakably(monkeypatch):
    def boom(q):
        raise WebUnavailable("search backend unreachable: TimeoutError")
    monkeypatch.setattr(web, "_load_brave_key", lambda: None)
    monkeypatch.setattr(web, "_ddg_search", boom)
    with pytest.raises(WebUnavailable, match="unreachable"):
        web.search("weather brisbane")


def test_ddg_redirect_unwrap():
    wrapped = ("/l/?uddg=https%3A%2F%2Fwww.bom.gov.au%2Fqld%2F&rut=abc")
    assert web._ddg_url(wrapped) == "https://www.bom.gov.au/qld/"
    assert web._ddg_url("https://direct.example/x") == "https://direct.example/x"


def test_search_backend_seam():
    out = web.search("x", backend=lambda q: [{"title": "t", "url": "u",
                                             "snippet": "s"}])
    assert out[0]["title"] == "t"


def test_malformed_port_is_speakable_refusal():
    for url in ("https://example.com:not-a-port/",
                "http://example.com:99999/"):
        with pytest.raises(WebRefused):
            validate_url(url, resolver_for(PUBLIC))
