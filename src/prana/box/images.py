"""Pictures on the box (Suti 2026-09-10: "put the box on the path").

The box's panel is 320×240 RGB565 and its firmware renders baked
scenes; an arbitrary picture reaches it as a small JPEG over the
LiveKit data channel of the device room, sent through the server API
(no room membership needed — the same path the room watchdog uses for
the Kali summons). The firmware reassembles the chunks, decodes with
esp_new_jpeg and shows the picture above the scene for a while.

Wire (topics; the firmware trusts them from an agent OR the server
API — presentation only, never the honest indicators):
  narada.image        JSON  {"type":"image","id":N,"size":B,"chunks":K,"ttl_ms":T}
  narada.image.chunk  bytes u16 id, u16 seq, u16 total (little-endian) + data
"""

from __future__ import annotations

import io
import json
import logging
import os
import struct
import time
from pathlib import Path

logger = logging.getLogger(__name__)

PANEL = (320, 240)
MAX_JPEG_BYTES = 48_000          # a few chunks; the box holds it in PSRAM
CHUNK_BYTES = 12_000             # under LiveKit's per-packet ceiling
TOPIC_IMAGE = "narada.image"
TOPIC_CHUNK = "narada.image.chunk"
DEVICE_ROOM = os.environ.get("NARADA_VOICE_ROOM", "narada-body")
IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".webp")


def images_dir() -> Path:
    return Path(os.environ.get("NARADA_IMAGES_DIR", str(Path.home() / ".narada" / "brain" / "images")))


def codex_home() -> Path:
    return Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex")))


def prepare_for_box(source: Path, panel: tuple[int, int] = PANEL,
                    max_bytes: int = MAX_JPEG_BYTES) -> bytes:
    """Fit the picture into the panel (letterboxed on near-black) as a
    baseline JPEG small enough for the data channel."""
    from PIL import Image

    with Image.open(source) as im:
        im = im.convert("RGB")
        im.thumbnail(panel, Image.LANCZOS)
        canvas = Image.new("RGB", panel, (8, 10, 14))
        canvas.paste(im, ((panel[0] - im.width) // 2, (panel[1] - im.height) // 2))
    for quality in (85, 75, 65, 55, 45, 35):
        buf = io.BytesIO()
        canvas.save(buf, format="JPEG", quality=quality, optimize=True, progressive=False)
        if buf.tell() <= max_bytes:
            return buf.getvalue()
    return buf.getvalue()


def chunk_image(jpeg: bytes, image_id: int, ttl_ms: int,
                chunk: int = CHUNK_BYTES) -> list[tuple[str, bytes]]:
    """(topic, payload) packets: the header, then the chunks in order."""
    image_id &= 0xFFFF
    parts = [jpeg[i:i + chunk] for i in range(0, len(jpeg), chunk)] or [b""]
    packets: list[tuple[str, bytes]] = [(TOPIC_IMAGE, json.dumps({
        "type": "image", "id": image_id, "size": len(jpeg),
        "chunks": len(parts), "ttl_ms": int(ttl_ms)}).encode("utf-8"))]
    for seq, part in enumerate(parts):
        packets.append((TOPIC_CHUNK, struct.pack("<HHH", image_id, seq, len(parts)) + part))
    return packets


def _livekit_api():
    from livekit import api as lkapi

    from prana.brain.voice import load_livekit_env

    env = {**load_livekit_env(), **{k: v for k, v in os.environ.items() if k.startswith("LIVEKIT_")}}
    url, key, secret = env.get("LIVEKIT_URL"), env.get("LIVEKIT_API_KEY"), env.get("LIVEKIT_API_SECRET")
    if not (url and key and secret):
        raise RuntimeError("LIVEKIT_URL/API_KEY/SECRET missing (~/.narada/.livekit.env)")
    return lkapi, lkapi.LiveKitAPI(url, key, secret)


async def send_to_box(jpeg: bytes, ttl_ms: int = 20_000, room: str = DEVICE_ROOM) -> int:
    """Publish the picture to the device room through the server API.
    Returns the image id used."""
    image_id = int(time.time()) & 0xFFFF
    lkapi, lk = _livekit_api()
    try:
        for topic, payload in chunk_image(jpeg, image_id, ttl_ms):
            await lk.room.send_data(lkapi.SendDataRequest(
                room=room, data=payload, topic=topic,
                kind=lkapi.DataPacket.Kind.RELIABLE))
    finally:
        await lk.aclose()
    return image_id


def resolve_image(name: str) -> Path:
    """A name in the brain's store (as in /api/images/<name>) or an
    absolute path to a picture file. Anything else is refused."""
    name = name.strip()
    if not name:
        raise FileNotFoundError("no image named")
    candidate = Path(name)
    if candidate.is_absolute():
        if candidate.suffix.lower() not in IMAGE_EXTS or not candidate.is_file():
            raise FileNotFoundError(name)
        return candidate
    # A bare name, or the store's own url form (/api/images/<name>);
    # any other path shape is refused rather than guessed at.
    if name.startswith("/api/images/"):
        name = name[len("/api/images/"):]
    if "/" in name or "\\" in name or ".." in name:
        raise FileNotFoundError(name)
    path = images_dir() / name
    if path.suffix.lower() not in IMAGE_EXTS or not path.is_file():
        raise FileNotFoundError(name)
    return path


def latest_generated(max_age_s: float = 15 * 60) -> Path | None:
    """The newest picture Codex generated recently (any thread), for
    "draw X and show it on the box" within one turn."""
    root = codex_home() / "generated_images"
    if not root.is_dir():
        return None
    newest: tuple[float, Path] | None = None
    cutoff = time.time() - max_age_s
    try:
        for thread in root.iterdir():
            if not thread.is_dir():
                continue
            for f in thread.iterdir():
                if f.suffix.lower() not in IMAGE_EXTS:
                    continue
                mtime = f.stat().st_mtime
                if mtime < cutoff:
                    continue
                if newest is None or mtime > newest[0]:
                    newest = (mtime, f)
    except OSError:
        return None
    return newest[1] if newest else None


async def show_image(name: str = "", ttl_s: int = 20) -> dict:
    """Show a picture on the box: a stored image by name, a path, or
    (with no name) the newest one Codex generated."""
    if name.strip():
        source = resolve_image(name)
    else:
        source = latest_generated()
        if source is None:
            raise FileNotFoundError("no recently generated image to show")
    jpeg = prepare_for_box(source)
    ttl_ms = max(3, min(int(ttl_s), 300)) * 1000
    image_id = await send_to_box(jpeg, ttl_ms)
    logger.info("box image %s: %s → %d bytes, ttl %ds", image_id, source.name, len(jpeg), ttl_s)
    return {"ok": True, "image": source.name, "bytes": len(jpeg), "ttl_s": ttl_ms // 1000,
            "room": DEVICE_ROOM}
