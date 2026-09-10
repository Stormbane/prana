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


def test_resolve_image_only_from_the_store_or_an_absolute_picture(tmp_path, monkeypatch):
    monkeypatch.setenv("NARADA_IMAGES_DIR", str(tmp_path / "store"))
    (tmp_path / "store").mkdir()
    _png(tmp_path / "store" / "abc.png", (8, 8))
    assert images.resolve_image("abc.png") == tmp_path / "store" / "abc.png"
    assert images.resolve_image("/api/images/abc.png") == tmp_path / "store" / "abc.png"
    for bad in ("", "nope.png", "../wake.md", "..\\x.png", "sub/abc.png"):
        with pytest.raises(FileNotFoundError):
            images.resolve_image(bad)
    _png(tmp_path / "elsewhere.png", (8, 8))
    assert images.resolve_image(str(tmp_path / "elsewhere.png")) == tmp_path / "elsewhere.png"
    (tmp_path / "secret.txt").write_text("x")
    with pytest.raises(FileNotFoundError):
        images.resolve_image(str(tmp_path / "secret.txt"))


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
