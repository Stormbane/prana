"""Pictures on the box (2026-09-10): fit to the panel as a small JPEG,
chunked for the data channel; names resolved safely."""

from __future__ import annotations

import json
import struct
import time

import pytest

from prana.box import images

PIL = pytest.importorskip("PIL")


def _png(path, size=(1024, 768), color=(200, 40, 40)):
    from PIL import Image

    Image.new("RGB", size, color).save(path)


def test_prepare_fits_the_panel_and_stays_small(tmp_path):
    from PIL import Image

    src = tmp_path / "big.png"
    _png(src, (2048, 512))
    jpeg = images.prepare_for_box(src)
    assert jpeg[:2] == b"\xff\xd8" and len(jpeg) <= images.MAX_JPEG_BYTES
    with Image.open(__import__("io").BytesIO(jpeg)) as im:
        assert im.size == images.PANEL  # letterboxed, never resampled off-panel
        r, g, b = im.getpixel((160, 5))  # the letterbox band, not the red picture
        assert r < 40 and g < 40 and b < 40


def test_chunks_carry_a_header_and_ordered_parts():
    jpeg = bytes(range(256)) * 100  # 25,600 bytes
    packets = images.chunk_image(jpeg, image_id=70000, ttl_ms=15000, chunk=12_000)
    topic, header = packets[0]
    assert topic == images.TOPIC_IMAGE
    h = json.loads(header)
    assert h == {"type": "image", "id": 70000 & 0xFFFF, "size": 25600, "chunks": 3,
                 "chunk": 12_000, "ttl_ms": 15000}
    body = b""
    for seq, (t, payload) in enumerate(packets[1:]):
        assert t == images.TOPIC_CHUNK
        pid, pseq, total = struct.unpack("<HHH", payload[:6])
        assert (pid, pseq, total) == (70000 & 0xFFFF, seq, 3)
        assert len(payload) - 6 <= 12_000
        body += payload[6:]
    assert body == jpeg


def test_resolve_image_only_from_the_store(tmp_path, monkeypatch):
    monkeypatch.setenv("NARADA_IMAGES_DIR", str(tmp_path / "store"))
    (tmp_path / "store").mkdir()
    _png(tmp_path / "store" / "abc.png", (8, 8))
    assert images.resolve_image("abc.png") == tmp_path / "store" / "abc.png"
    assert images.resolve_image("/api/images/abc.png") == tmp_path / "store" / "abc.png"
    _png(tmp_path / "elsewhere.png", (8, 8))
    for bad in ("", "nope.png", "../wake.md", "..\\x.png", "sub/abc.png", "Abc.PNG",
                str(tmp_path / "elsewhere.png"), "/api/images/C:\\private\\photo.png",
                "C:/private/photo.png"):
        with pytest.raises(FileNotFoundError):
            images.resolve_image(bad)  # no paths, ever (review)


def test_latest_generated_is_the_newest_recent_picture(tmp_path, monkeypatch):
    import os

    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    assert images.latest_generated() is None
    root = tmp_path / "generated_images"
    (root / "t1").mkdir(parents=True)
    (root / "t2").mkdir(parents=True)
    old = root / "t1" / "old.png"
    _png(old, (8, 8))
    os.utime(old, (time.time() - 3600, time.time() - 3600))
    new = root / "t2" / "new.png"
    _png(new, (8, 8))
    (root / "t2" / "notes.txt").write_text("x")
    assert images.latest_generated() == new
    assert images.latest_generated(max_age_s=1e-9) is None


def test_show_image_sends_the_prepared_picture(tmp_path, monkeypatch):
    import asyncio

    monkeypatch.setenv("NARADA_IMAGES_DIR", str(tmp_path))
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "no-codex"))  # nothing generated here
    _png(tmp_path / "leaf.png", (640, 480))
    sent = {}

    async def fake_send(jpeg, ttl_ms, room=images.DEVICE_ROOM):
        sent["jpeg"] = jpeg
        sent["ttl_ms"] = ttl_ms
        return 7

    monkeypatch.setattr(images, "send_to_box", fake_send)
    out = asyncio.run(images.show_image("leaf.png", ttl_s=900))
    assert out["ok"] and out["image"] == "leaf.png" and out["ttl_s"] == 300  # clamped
    assert sent["jpeg"][:2] == b"\xff\xd8" and sent["ttl_ms"] == 300_000
    with pytest.raises(FileNotFoundError):
        asyncio.run(images.show_image("", 5))  # nothing generated recently


def test_the_mcp_tool_works_on_a_running_loop(tmp_path, monkeypatch):
    """FastMCP runs tools on its own loop; the tool must await, not
    asyncio.run (the first live turn failed exactly there)."""
    import asyncio

    from prana.box import mcp as box_mcp

    async def fake_show(name, ttl_s):
        return {"ok": True, "image": name or "latest", "ttl_s": ttl_s}

    monkeypatch.setattr(images, "show_image", fake_show)

    async def go():
        return json.loads(await box_mcp.box_show_image("", 12))
    assert asyncio.run(go()) == {"ok": True, "image": "latest", "ttl_s": 12}

    async def boom(name, ttl_s):
        raise FileNotFoundError("nothing recent")

    monkeypatch.setattr(images, "show_image", boom)

    async def go2():
        return json.loads(await box_mcp.box_show_image("", 12))
    assert asyncio.run(go2())["ok"] is False


def test_transfers_are_serialised_and_ids_do_not_repeat(tmp_path, monkeypatch):
    import asyncio

    monkeypatch.setenv("NARADA_IMAGES_DIR", str(tmp_path))
    log: list[tuple[str, int]] = []

    class _Room:
        async def send_data(self, req):
            log.append((req.topic, req.data[:2].hex() if req.topic == images.TOPIC_CHUNK else "hdr"))
            await asyncio.sleep(0.01)  # let the other transfer try to interleave

    class _LK:
        room = _Room()

        async def aclose(self):
            pass

    class _Req:
        def __init__(self, room, data, topic, kind):
            self.topic, self.data = topic, data

    class _API:
        SendDataRequest = _Req

        class DataPacket:
            class Kind:
                RELIABLE = 1

    monkeypatch.setattr(images, "_livekit_api", lambda: (_API, _LK()))
    a = b"\xff\xd8" + bytes(30_000)
    b = b"\xff\xd8" + bytes(30_000)

    async def go():
        return await asyncio.gather(images.send_to_box(a, 1000), images.send_to_box(b, 1000))
    ids = asyncio.run(go())
    assert ids[0] != ids[1]
    # every packet of one transfer lands before any packet of the other
    topics = [t for t, _ in log]
    first_header = topics.index(images.TOPIC_IMAGE)
    second_header = topics.index(images.TOPIC_IMAGE, first_header + 1)
    assert topics[first_header + 1:second_header] == [images.TOPIC_CHUNK] * 3
    assert not (tmp_path / ".box-send.lock").exists()


def test_the_endpoint_refuses_paths_dressed_as_store_urls(tmp_path, monkeypatch):
    from starlette.testclient import TestClient

    from prana.brain.api import create_app
    from prana.brain.config import BrainConfig

    wake = tmp_path / "wake.md"
    wake.write_text("ctx", encoding="utf-8")
    config = BrainConfig(sessions_root=tmp_path / "sessions", wake_context=wake,
                         images_dir=tmp_path / "images")
    monkeypatch.setattr("prana.brain.api.load_brain_tokens", lambda: {"app": "tok-app"})
    shown = []

    async def fake_show(name, ttl_s):
        shown.append((name, ttl_s))
        return {"ok": True}

    monkeypatch.setattr(images, "show_image", fake_show)

    class _NoBackend:
        def __init__(self, **kw):  # pragma: no cover
            raise AssertionError("no session here")

    with TestClient(create_app(config, _NoBackend)) as c:
        h = {"Authorization": "Bearer tok-app"}
        for bad in ("/api/images/C:\\private\\photo.png", "/api/images/../wake.md",
                    "https://x/api/images/abc.png", "/api/images/Abc.PNG", "abc.png", 7):
            assert c.post("/api/box/show", headers=h, json={"url": bad}).status_code == 400, bad
        assert c.post("/api/box/show", headers=h, json={"url": "/api/images/abc12.png", "ttl_s": 9}
                      ).status_code == 200
        assert shown == [("abc12.png", 9)]


def test_the_registered_tool_runs_through_fastmcp(monkeypatch):
    import asyncio

    from prana.box import mcp as box_mcp

    async def fake_show(name, ttl_s):
        return {"ok": True, "image": name or "latest", "ttl_s": ttl_s, "delivery": "published"}

    monkeypatch.setattr(images, "show_image", fake_show)

    async def go():
        return await box_mcp.mcp.call_tool("box_show_image", {"name": "", "ttl_s": 7})
    result = asyncio.run(go())
    text = result[0].text if isinstance(result, list) else str(result)
    assert '"delivery": "published"' in text and '"latest"' in text
