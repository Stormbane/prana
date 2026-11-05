"""The OpenAI-shaped front door (spec §1a wire contract).

Baseline: strict ``/v1/chat/completions`` — an unmodified OpenAI SDK
must work; tool use is internal; baseline clients see text. Narada
extensions ride ONLY in the request's ``narada`` object (``session_id``,
``request_id``); response extensions wait for docs/contracts/
brain-wire-v1.md (MVP ships baseline only).

Turn execution is DETACHED from the HTTP response: the agent turn runs
in its own task and always drains to a terminal state + transcript
write, while the response merely taps its event queue. This is what
makes "disconnect ≠ cancel" true rather than aspirational.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse

from prana.brain.cards import ActivityEvent, CardEvent
from prana.brain.config import MODELS, BrainConfig, backend_for
from prana.brain.session import (
    ModelConflict,
    SwitchRefused,
    BrainSession,
    SessionBusy,
    SessionPool,
    valid_session_id,
)
from prana.brain.tokens import load_brain_tokens, tier_for_token
from prana.brain.turns import TERMINAL_STATES, fingerprint

logger = logging.getLogger(__name__)


# ── OpenAI wire helpers ─────────────────────────────────────────────────


def _openai_error(status: int, message: str,
                  err_type: str = "invalid_request_error",
                  headers: dict | None = None) -> JSONResponse:
    return JSONResponse(
        status_code=status,
        headers=headers,
        content={"error": {"message": message, "type": err_type}},
    )


def _completion_body(model: str, text: str, completion_id: str) -> dict:
    return {
        "id": completion_id,
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [{
            "index": 0,
            "message": {"role": "assistant", "content": text},
            "finish_reason": "stop",
        }],
        "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
    }


def _chunk(model: str, completion_id: str, delta: dict,
           finish: str | None = None, card: dict | None = None,
           activity: dict | None = None) -> str:
    payload: dict = {
        "id": completion_id,
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "model": model,
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
    }
    if card is not None:
        payload["narada_card"] = card
    if activity is not None:
        payload["narada_activity"] = activity
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"


def _stream_error(message: str) -> str:
    # OpenAI streams carry errors as a data event with an `error` object;
    # explicit, never dressed as a normal completion (spec §1a bounds).
    return ("data: "
            + json.dumps({"error": {"message": message, "type": "server_error"}})
            + "\n\n")


class UnsupportedImage(ValueError):
    """An image part the server can't accept — reported as a clear 400,
    never silently dropped (phone-app compat ask, 2026-09-06)."""


def _parse_data_uri(url: str) -> dict:
    """`data:image/jpeg;base64,...` → a Claude image source block.
    Remote URLs are refused: the brain does not fetch on a client's
    behalf."""
    if not url.startswith("data:"):
        raise UnsupportedImage(
            "image_url must be a data: URI (the server does not fetch "
            "remote images)")
    try:
        header, data = url.split(",", 1)
        media_type = header[len("data:"):].split(";", 1)[0]
    except ValueError:
        raise UnsupportedImage("malformed data: URI in image_url") from None
    if not header.endswith(";base64") or not media_type.startswith("image/"):
        raise UnsupportedImage(
            "image_url data: URI must be base64-encoded with an image/* "
            "media type")
    return {"type": "base64", "media_type": media_type, "data": data}


def _last_user_content(messages: list) -> tuple[str, list[dict]] | None:
    """Returns (text, image_sources) from the newest user message, or
    None. Raises UnsupportedImage for image parts we must not drop."""
    for msg in reversed(messages):
        if isinstance(msg, dict) and msg.get("role") == "user":
            content = msg.get("content")
            if isinstance(content, str) and content.strip():
                return content, []
            if isinstance(content, list):  # multimodal array form
                texts: list[str] = []
                images: list[dict] = []
                for p in content:
                    if not isinstance(p, dict):
                        continue
                    if p.get("type") == "text" and p.get("text"):
                        texts.append(p["text"])
                    elif p.get("type") == "image_url":
                        url = (p.get("image_url") or {}).get("url", "")
                        images.append(_parse_data_uri(url))
                if texts or images:
                    return "\n".join(texts), images
    return None


def _content_blocks(text: str, images: list[dict]) -> list[dict] | str:
    """Claude content blocks for a multimodal turn; plain string when
    there are no images (the SDK fast path)."""
    if not images:
        return text
    blocks: list[dict] = [{"type": "image", "source": src} for src in images]
    if text.strip():
        blocks.append({"type": "text", "text": text})
    return blocks


def _render_history(messages: list) -> str:
    """Flatten the OpenAI messages array into one prompt — stateless
    mode holds no server context by contract."""
    lines = []
    for msg in messages:
        if not isinstance(msg, dict):
            continue
        content = msg.get("content")
        if isinstance(content, list):
            content = "\n".join(
                p.get("text", "") for p in content
                if isinstance(p, dict) and p.get("type") == "text")
        if isinstance(content, str) and content.strip():
            lines.append(f"[{msg.get('role', 'user')}] {content}")
    return "\n\n".join(lines)


# ── Turn execution (detached from the response) ─────────────────────────


class TurnRun:
    """One executing turn: a detached task + a tap-able event queue.

    Events: ("delta", text) | ("end", state, full_text_or_error).
    The task always reaches a terminal state and persists it, whether
    or not anyone is still reading the queue.
    """

    def __init__(self) -> None:
        self.queue: asyncio.Queue = asyncio.Queue()
        self.state: str | None = None
        self.text: str = ""
        self.error: str | None = None
        self.task: asyncio.Task | None = None
        self.cards: list[dict] = []

    def _emit(self, event: tuple) -> None:
        self.queue.put_nowait(event)


async def _execute_turn(run: TurnRun, session: BrainSession, pool: SessionPool,
                        content, transcript_text: str,
                        request_id: str | None, deadline_s: float) -> None:
    """`content`: string or Claude content blocks for the backend;
    `transcript_text` is what lands in the transcript (image bytes
    summarized, never dumped)."""
    parts: list[str] = []

    async def on_start() -> None:
        # Acceptance already happened synchronously in the handler
        # (atomic with the reservation); here the turn merely starts.
        if request_id is not None:
            session.turns.transition(request_id, "running")
        session.append_transcript("user", transcript_text)

    def finish(state: str, result: str) -> None:
        run.state, run.text = state, "".join(parts)
        if state != "completed":
            run.error = result
        if request_id is not None:
            session.turns.transition(request_id, state, result, cards=run.cards)
        if state == "completed":
            session.append_transcript("assistant", result)
        else:
            session.append_transcript("system", f"turn {state}: {result}")
        run._emit(("end", state, result))

    try:
        # The deadline covers semaphore queueing too — a turn stuck
        # behind a full pool must still end explicitly (recheck P2).
        try:
            await asyncio.wait_for(pool.turn_semaphore.acquire(),
                                   timeout=deadline_s)
        except asyncio.TimeoutError:
            raise TimeoutError(
                f"turn queued behind a full pool for {deadline_s:.0f}s"
            ) from None
        try:
            gen = session.run_turn(content, deadline_s=deadline_s,
                                   on_start=on_start)
            async for chunk in gen:
                if isinstance(chunk, CardEvent):
                    run.cards.append(chunk.spec)
                    run._emit(("card", chunk.spec))
                    continue
                if isinstance(chunk, ActivityEvent):
                    # Live only: chips are the turn's motion, not its
                    # record — a replay carries text and cards.
                    run._emit(("activity", chunk.activity))
                    continue
                parts.append(chunk)
                run._emit(("delta", chunk))
        finally:
            pool.turn_semaphore.release()
    except TimeoutError as exc:
        finish("failed", str(exc))
        return
    except Exception as exc:
        logger.exception("turn failed (session=%s)", session.session_id)
        finish("failed", repr(exc))
        return
    finally:
        session.release()
    text = "".join(parts)
    if session.was_cancelled:
        finish("cancelled", text)
    else:
        finish("completed", text)


# ── App factory ─────────────────────────────────────────────────────────


def create_app(config: BrainConfig, backend_factory,
               backends: dict | None = None, *,
               probe_tools: bool = False) -> FastAPI:
    """``backend_factory`` builds the default (Claude) backend;
    ``backends`` maps other backend names from the model registry to
    their factories — the server passes Codex, tests pass nothing and
    every model gets the fake. ``probe_tools``: handshake every tool
    server once at startup, in the background (the real server says
    yes; tests spawn nothing)."""
    from contextlib import asynccontextmanager

    pool = SessionPool(config, backend_factory, backends)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        pool.start_reaper()
        probe_task = None
        if probe_tools:
            # Suti 2026-09-16: "why didn't we know it was down already?"
            # — a dead tool server is in the feed minutes after the
            # brain starts, not when the next weight goes unlogged.
            from prana.brain import mcp_probe

            probe_task = asyncio.get_running_loop().create_task(
                asyncio.to_thread(mcp_probe.check, pool.configured_mcp(), "brain startup"))
        yield
        if probe_task is not None:
            probe_task.cancel()
        await pool.close()

    app = FastAPI(title="narada-brain", docs_url=None, redoc_url=None,
                  lifespan=lifespan)
    # Browser transport contract (spec Layer 2): explicit allowlist,
    # explicit headers, preflight handled by the middleware. CORS
    # constrains browsers only — bearer auth stays the authorization.
    from fastapi.middleware.cors import CORSMiddleware

    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(config.cors_origins),
        allow_methods=["GET", "POST"],
        allow_headers=["content-type", "authorization"],
    )
    # Static PWA at /app on the brain's own origin (same-origin with
    # the API — the installed app needs no CORS). Mounted only when the
    # deploy has published assets (deploy/publish-pwa.ps1).
    if config.static_dir.is_dir():
        from fastapi.staticfiles import StaticFiles

        app.mount("/app", StaticFiles(directory=str(config.static_dir),
                                      html=True), name="pwa")

    tokens = load_brain_tokens()
    app.state.pool = pool

    # Auth rate limit (spec §1a: "repeated failures rate-limit"):
    # per-address sliding count; lockout after the threshold.
    auth_failures: dict[str, list[float]] = {}
    AUTH_FAIL_LIMIT, AUTH_FAIL_WINDOW_S = 10, 60.0

    def _auth_failed(addr: str) -> None:
        now = time.monotonic()
        window = [t for t in auth_failures.get(addr, ())
                  if now - t < AUTH_FAIL_WINDOW_S]
        window.append(now)
        auth_failures[addr] = window
        logger.warning("auth failure from %s (%d in window)", addr, len(window))

    def _auth_locked(addr: str) -> bool:
        now = time.monotonic()
        window = [t for t in auth_failures.get(addr, ())
                  if now - t < AUTH_FAIL_WINDOW_S]
        auth_failures[addr] = window
        return len(window) >= AUTH_FAIL_LIMIT

    async def require_tier(request: Request) -> str:
        addr = request.client.host if request.client else "?"
        if _auth_locked(addr):
            raise HTTPException(
                429, "too many authentication failures",
                headers={"Retry-After": str(int(AUTH_FAIL_WINDOW_S))})
        header = request.headers.get("authorization", "")
        if not header.startswith("Bearer "):
            _auth_failed(addr)
            raise HTTPException(401, "missing bearer token")
        tier = tier_for_token(header[len("Bearer "):].strip(), tokens)
        if tier is None:
            _auth_failed(addr)
            raise HTTPException(401, "invalid token")
        return tier

    @app.get("/health")
    async def health() -> dict:
        return {"ok": True, **pool.stats()}

    @app.get("/v1/models")
    async def models(tier: str = Depends(require_tier)) -> dict:
        return {
            "object": "list",
            "data": [{"id": m, "object": "model", "created": 0,
                      "owned_by": "narada", "backend": backend_for(m),
                      "default": m == config.model} for m in MODELS],
        }

    # ── The box's screen: show a stored picture there ─────────────────
    @app.post("/api/box/show")
    async def box_show(request: Request, tier: str = Depends(require_tier)) -> dict:
        """The app's "On the box" button: the picture behind an image
        card, sent to the device room (prana.box.images)."""
        from prana.box import images as box_images

        try:
            body = await request.json()
        except Exception:
            raise HTTPException(400, "body must be JSON")
        url = body.get("url") if isinstance(body, dict) else None
        # The GET route's strict name rule, resolved beneath the store
        # (review: "/api/images/C:\\x.png" split to an absolute path).
        name = url.rsplit("/", 1)[-1] if isinstance(url, str) and url.startswith("/api/images/") else ""
        if not box_images.STORE_NAME.fullmatch(name):
            raise HTTPException(400, "url must name an image in the store")
        ttl = body.get("ttl_s", 20)
        try:
            return await box_images.show_image(name, int(ttl) if isinstance(ttl, (int, float)) else 20)
        except FileNotFoundError:
            raise HTTPException(404, "no such image")
        except Exception as exc:  # noqa: BLE001
            logger.warning("box show failed: %s", exc)
            raise HTTPException(502, f"could not reach the box: {exc}")

    # ── Images Narada generated (codex_backend.harvest_images) ───────
    @app.get("/api/images/{name}")
    async def image(name: str, tier: str = Depends(require_tier)):
        import re as _re

        from fastapi.responses import FileResponse

        if not _re.fullmatch(r"[a-z0-9-]{1,64}\.(png|jpg|jpeg|webp)", name):
            raise HTTPException(400, "bad image name")
        path = config.images_dir / name
        if not path.is_file():
            raise HTTPException(404, "no such image")
        return FileResponse(str(path))

    # ── Phone-app voice (spec: the one prana-side proxy) ─────────────
    # GET session → {room, token, identity, url, agent}; the app joins,
    # THEN POSTs dispatch {room}. Join-before-dispatch is the client's
    # contract (exactly once per room — dispatch has no idempotency).
    @app.get("/api/voice/session")
    async def voice_session(tier: str = Depends(require_tier)) -> dict:
        from prana.brain import voice

        try:
            return voice.phone_session()
        except RuntimeError as exc:
            raise HTTPException(503, str(exc))

    @app.post("/api/voice/dispatch")
    async def voice_dispatch(request: Request,
                             tier: str = Depends(require_tier)) -> dict:
        from prana.brain import voice

        try:
            body = await request.json()
        except Exception:
            raise HTTPException(400, "body must be JSON")
        room = body.get("room", "") if isinstance(body, dict) else ""
        if not isinstance(room, str) or not room:
            raise HTTPException(400, "missing room")
        # Context pack (plan §T3a): the app names the brain session the
        # call should read; the worker reads that session's transcript
        # under the caller's tier — never another tier's.
        session_id = body.get("session_id") if isinstance(body, dict) else None
        metadata = None
        if isinstance(session_id, str) and valid_session_id(session_id):
            metadata = json.dumps({"session_id": session_id, "tier": tier})
        try:
            return await asyncio.to_thread(
                voice.dispatch_agent, room, metadata=metadata)
        except ValueError as exc:
            raise HTTPException(403, str(exc))
        except RuntimeError as exc:
            raise HTTPException(503, str(exc))
        except Exception as exc:
            logger.warning("voice dispatch failed for %s: %s", room, exc)
            raise HTTPException(502, f"dispatch failed: {exc}")

    # ── Voice prefs (operator-tunable VAD/noise/language knobs) ──────
    # The phone worker reads ~/.narada/brain/voice-prefs.json at each
    # job start, so a change here applies to the NEXT call, no restart.
    _VOICE_PREFS = Path.home() / ".narada" / "brain" / "voice-prefs.json"
    _NOISE_MODES = ("near_field", "far_field", "off")
    _BREVITY_MODES = ("normal", "brief")
    # The realtime model for calls (Suti 2026-09-09: "let me choose the
    # voice model from settings"). First is the worker's default.
    from prana.voice.prefs import (VOICE_MODELS as _VOICE_MODELS, VOICE_OPTIONS,
                                   selected_model, selected_voice)
    _VOICE_MODEL_DEFAULT = selected_model({})

    @app.get("/api/voice/prefs")
    async def voice_prefs_get(tier: str = Depends(require_tier)) -> dict:
        from prana.voice.prefs import read_prefs

        saved = read_prefs(_VOICE_PREFS)
        return {
            "vad_threshold": float(saved.get("vad_threshold", 0.7)),
            "noise_reduction": saved.get("noise_reduction", "near_field"),
            "language": saved.get("language", "en"),
            # Brief mode (Suti on the phone 2026-09-08: "like TV mode —
            # toggle it from the phone"): short replies on calls.
            "brevity": saved.get("brevity", "normal"),
            "model": saved.get("model") or _VOICE_MODEL_DEFAULT,
            "models": list(dict.fromkeys((_VOICE_MODEL_DEFAULT, *_VOICE_MODELS))),
            "scope": "app_and_box",
            "live_voice": selected_voice(saved, "gpt-live-1"),
            "realtime_voice": selected_voice(saved, "gpt-realtime"),
            "gemini_voice": selected_voice(saved, "gemini-3.8-live"),
            "voice_options": VOICE_OPTIONS,
        }

    @app.post("/api/voice/prefs")
    async def voice_prefs_set(request: Request,
                              tier: str = Depends(require_tier)) -> dict:
        try:
            body = await request.json()
        except Exception:
            raise HTTPException(400, "body must be JSON")
        if not isinstance(body, dict):
            raise HTTPException(400, "body must be an object")
        prefs: dict = {}
        if "vad_threshold" in body:
            try:
                v = float(body["vad_threshold"])
            except (TypeError, ValueError):
                raise HTTPException(400, "vad_threshold must be a number")
            if not 0.2 <= v <= 0.95:
                raise HTTPException(400, "vad_threshold must be 0.2–0.95")
            prefs["vad_threshold"] = v
        if "noise_reduction" in body:
            if body["noise_reduction"] not in _NOISE_MODES:
                raise HTTPException(
                    400, f"noise_reduction must be one of {_NOISE_MODES}")
            prefs["noise_reduction"] = body["noise_reduction"]
        if "language" in body:
            lang = str(body["language"]).strip().lower()
            if not (len(lang) == 2 and lang.isalpha()):
                raise HTTPException(400, "language must be ISO-639-1 (e.g. en)")
            prefs["language"] = lang
        if "brevity" in body:
            if body["brevity"] not in _BREVITY_MODES:
                raise HTTPException(
                    400, f"brevity must be one of {_BREVITY_MODES}")
            prefs["brevity"] = body["brevity"]
        if "model" in body:
            allowed = (_VOICE_MODEL_DEFAULT, *_VOICE_MODELS)
            if body["model"] not in allowed:
                raise HTTPException(400, f"model must be one of {allowed}")
            prefs["model"] = body["model"]
        for key, options in VOICE_OPTIONS.items():
            if key in body:
                if body[key] not in [option["id"] for option in options]:
                    raise HTTPException(400, f"Unsupported {key}")
                prefs[key] = body[key]
        # One writer for the file, shared with the voice tool (review:
        # two independent read-modify-writes lost updates).
        from prana.voice.prefs import PrefsError, update_prefs

        try:
            saved = await asyncio.to_thread(update_prefs, prefs, _VOICE_PREFS)
        except PrefsError as exc:
            raise HTTPException(503, str(exc))
        return {"saved": saved, "applies": "next call"}

    # ── Questling v1: today's cards + tap answers (akhada asks) ──────
    from prana.brain.today_api import register_today
    register_today(app, require_tier)

    # The News card (redesign L2d): the newest edition the news desk wrote,
    # with whether it is stale and the last failure (prana/news.py).
    @app.get("/api/news")
    async def news(tier: str = Depends(require_tier)) -> dict:
        from prana import news as _news
        return await asyncio.to_thread(_news.latest)

    # API credits, checked nightly (prana/credits.py): the Data tab's card.
    @app.get("/api/credits")
    async def credits(tier: str = Depends(require_tier)) -> dict:
        from prana import credits as _credits
        return await asyncio.to_thread(_credits.latest)
    from prana.brain.doctor_api import register_doctor
    register_doctor(app, require_tier)

    # ── Fitness summary (the Data tab's read-only view) ──────────────
    @app.get("/api/fitness/summary")
    async def fitness_summary(day: str = "today",
                              tier: str = Depends(require_tier)) -> dict:
        """akhada's own tool-computed day summary (totals, targets,
        meals, workouts) — the same numbers the voice agent reads out,
        served to the app's Data tab. In-process read, no subprocess."""
        if day not in ("today", "yesterday") and not (
                len(day) == 10 and day[4] == "-" and day[7] == "-"):
            raise HTTPException(400, "day must be today/yesterday/YYYY-MM-DD")
        try:
            from akhada.store.db import Store
            from akhada.tools.catalog import dispatch
        except ImportError:
            raise HTTPException(503, "akhada not installed on this host")

        def _read() -> dict:
            return dispatch("get_today_summary", {"date": day},
                            store=Store(), session_id="brain-data-tab",
                            source="app")
        try:
            return await asyncio.to_thread(_read)
        except Exception as exc:  # noqa: BLE001
            logger.warning("fitness summary failed: %s", exc)
            raise HTTPException(502, f"summary failed: {exc}")

    @app.get("/api/fitness/recent")
    async def fitness_recent(days: int = 14,
                             tier: str = Depends(require_tier)) -> dict:
        """Per-day totals over the last N days — the Data tab's chart."""
        if not 1 <= days <= 60:
            raise HTTPException(400, "days must be 1–60")
        try:
            from akhada.store.db import Store
            from akhada.tools.catalog import dispatch
        except ImportError:
            raise HTTPException(503, "akhada not installed on this host")
        try:
            return await asyncio.to_thread(
                dispatch, "get_recent", {"days": days},
                store=Store(), session_id="brain-data-tab", source="app")
        except Exception as exc:  # noqa: BLE001
            logger.warning("fitness recent failed: %s", exc)
            raise HTTPException(502, f"recent failed: {exc}")

    @app.get("/api/fitness/exercise")
    async def fitness_exercise(name: str, days: int = 365,
                               tier: str = Depends(require_tier)) -> dict:
        """One exercise's sets over time (Suti 2026-09-09: tap the set
        card → that exercise, its weight/reps progression, the days it
        was logged) — akhada's get_exercise_history, in-process."""
        name = name.strip()
        if not name or len(name) > 80:
            raise HTTPException(400, "name must be 1–80 characters")
        if not 1 <= days <= 730:
            raise HTTPException(400, "days must be 1–730")
        try:
            from akhada.store.db import Store
            from akhada.tools.catalog import dispatch
        except ImportError:
            raise HTTPException(503, "akhada not installed on this host")
        try:
            return await asyncio.to_thread(
                dispatch, "get_exercise_history", {"exercise": name, "days": days},
                store=Store(), session_id="brain-data-tab", source="app")
        except Exception as exc:  # noqa: BLE001
            logger.warning("exercise history failed: %s", exc)
            raise HTTPException(502, f"exercise history failed: {exc}")

    # Mutations the Data tab may perform directly. A deliberate,
    # narrow allowlist: corrections to what exists (amend, void,
    # add-to-meal). Free-text logging goes through the chat agent —
    # parsing food is cognition, not a REST call.
    _FITNESS_ACTIONS = ("void_entry", "amend_entry", "add_meal_items")

    @app.post("/api/fitness/action")
    async def fitness_action(request: Request,
                             tier: str = Depends(require_tier)) -> dict:
        try:
            body = await request.json()
        except Exception:
            raise HTTPException(400, "body must be JSON")
        tool = body.get("tool") if isinstance(body, dict) else None
        args = body.get("args") if isinstance(body, dict) else None
        if tool not in _FITNESS_ACTIONS:
            raise HTTPException(403, f"tool must be one of {_FITNESS_ACTIONS}")
        if not isinstance(args, dict):
            raise HTTPException(400, "missing args")
        try:
            from akhada.store.db import Store
            from akhada.tools.catalog import dispatch
        except ImportError:
            raise HTTPException(503, "akhada not installed on this host")
        try:
            return await asyncio.to_thread(
                dispatch, tool, args,
                store=Store(), session_id="app-data-tab", source="app")
        except Exception as exc:  # noqa: BLE001
            logger.warning("fitness action %s failed: %s", tool, exc)
            raise HTTPException(502, f"{tool} failed: {exc}")

    # ── Notes (Keep-style; same store Narada's MCP tools edit) ───────
    @app.get("/api/notes")
    async def notes_list(tier: str = Depends(require_tier)) -> list:
        from prana.notes import store as notes_store

        return notes_store.list_notes()

    # ── the chat mirror ──────────────────────────────────────────────
    # The phone mirrors each conversation's local transcript here
    # (debounced, and again when it comes back online). The brain only
    # records turns that reached it; this is the copy of what the phone
    # holds, including messages that never got through. A copy, not the
    # record: last write wins, whole document, atomic on disk.
    _CHAT_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,128}$")
    _CHAT_BODY_MAX = 1_000_000

    def _chat_path(tier: str, chat_id: str) -> Path:
        return config.chats_root / tier / f"{chat_id}.json"

    @app.put("/api/chats/{chat_id}")
    async def chat_mirror_put(chat_id: str, request: Request,
                              tier: str = Depends(require_tier)) -> dict:
        if not _CHAT_ID_RE.match(chat_id):
            raise HTTPException(400, "bad chat id")
        raw = await request.body()
        if len(raw) > _CHAT_BODY_MAX:
            raise HTTPException(413, "chat too large to mirror")
        try:
            body = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            raise HTTPException(400, "body must be JSON")
        if not isinstance(body, dict) or not isinstance(body.get("messages"), list):
            raise HTTPException(400, "body must be an object with a messages list")
        doc = {**body, "id": chat_id, "tier": tier,
               "mirrored_at": datetime.now().isoformat(timespec="seconds")}
        path = _chat_path(tier, chat_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, path)  # atomic: never a torn chat on disk
        return {"ok": True, "id": chat_id, "messages": len(body["messages"]),
                "mirrored_at": doc["mirrored_at"]}

    @app.get("/api/chats")
    async def chat_mirror_list(tier: str = Depends(require_tier)) -> list:
        root = config.chats_root / tier
        out = []
        for p in sorted(root.glob("*.json")) if root.is_dir() else []:
            try:
                doc = json.loads(p.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            out.append({"id": doc.get("id", p.stem), "title": doc.get("title", ""),
                        "updatedAt": doc.get("updatedAt"), "mirrored_at": doc.get("mirrored_at"),
                        "messages": len(doc.get("messages") or [])})
        out.sort(key=lambda d: d.get("updatedAt") or 0, reverse=True)
        return out

    @app.get("/api/chats/{chat_id}")
    async def chat_mirror_get(chat_id: str, tier: str = Depends(require_tier)) -> dict:
        if not _CHAT_ID_RE.match(chat_id):
            raise HTTPException(400, "bad chat id")
        path = _chat_path(tier, chat_id)
        if not path.is_file():
            raise HTTPException(404, "no such chat")
        return json.loads(path.read_text(encoding="utf-8"))

    @app.post("/api/notes")
    async def notes_create(request: Request,
                           tier: str = Depends(require_tier)) -> dict:
        from prana.notes import store as notes_store

        try:
            body = await request.json()
        except Exception:
            raise HTTPException(400, "body must be JSON")
        if not isinstance(body, dict):
            raise HTTPException(400, "body must be an object")
        try:
            return notes_store.create_note(
                str(body.get("title", "")),
                kind=str(body.get("kind", "text")),
                text=str(body.get("text", "")),
                items=[str(t) for t in body.get("items", [])
                       if isinstance(t, str)],
                color=body["color"] if isinstance(body.get("color"), str) else None,
            )
        except ValueError as exc:
            raise HTTPException(400, str(exc))

    @app.patch("/api/notes/{note_id}")
    async def notes_update(note_id: str, request: Request,
                           tier: str = Depends(require_tier)) -> dict:
        from prana.notes import store as notes_store

        try:
            body = await request.json()
        except Exception:
            raise HTTPException(400, "body must be JSON")
        if not isinstance(body, dict):
            raise HTTPException(400, "body must be an object")
        kwargs: dict = {}
        if "title" in body:
            kwargs["title"] = str(body["title"])
        if "text" in body:
            kwargs["text"] = str(body["text"])
        if isinstance(body.get("add_items"), list):
            kwargs["add_items"] = [str(t) for t in body["add_items"]]
        if isinstance(body.get("remove_item_ids"), list):
            kwargs["remove_item_ids"] = [str(t) for t in body["remove_item_ids"]]
        if isinstance(body.get("check"), dict):
            kwargs["check"] = {str(k): bool(v)
                               for k, v in body["check"].items()}
        if body.get("color") is not None:
            # null = no change (like the store and the MCP tool); "none"
            # is the explicit clear (review: str(None) became "none").
            if not isinstance(body["color"], str):
                raise HTTPException(400, "color must be a string")
            kwargs["color"] = body["color"]
        try:
            return notes_store.update_note(note_id, **kwargs)
        except KeyError:
            raise HTTPException(404, "no such note")
        except ValueError as exc:
            raise HTTPException(400, str(exc))

    @app.delete("/api/notes/{note_id}")
    async def notes_delete(note_id: str,
                           tier: str = Depends(require_tier)) -> dict:
        from prana.notes import store as notes_store

        if not notes_store.delete_note(note_id):
            raise HTTPException(404, "no such note")
        return {"deleted": True}

    # ── Projects overview (the Data tab's Projects card) ─────────────
    @app.get("/api/projects")
    async def projects(tier: str = Depends(require_tier)) -> list:
        from prana.brain import projects as _projects

        return await asyncio.to_thread(_projects.list_projects)

    # ── Notification feed (the pinned Notifications chat) ────────────
    @app.get("/api/notifications")
    async def notifications(tier: str = Depends(require_tier)) -> list:
        from prana.brain import push

        return push.read_notifications()

    # The producer door (plan §T7): the host, the watchdog's
    # reconciliation, notify-suti — anything holding a prana-tier
    # token — emits a level-tagged notification; routing is decided
    # HERE, never by the producer.
    @app.post("/v1/narada/notify")
    async def narada_notify(request: Request,
                            tier: str = Depends(require_tier)) -> dict:
        if tier != "prana":
            raise HTTPException(403, "notify is prana-tier only")
        from prana.brain import push

        try:
            body = await request.json()
        except Exception:
            raise HTTPException(400, "body must be JSON")
        if not isinstance(body, dict):
            raise HTTPException(400, "body must be an object")
        text = body.get("body")
        if not isinstance(text, str) or not text.strip():
            raise HTTPException(400, "missing body")
        level = body.get("level", push.DEFAULT_LEVEL)
        if level not in push.LEVELS:
            raise HTTPException(400, f"level must be one of {push.LEVELS}")
        delivered = body.get("delivered_channels") or []
        if not isinstance(delivered, list):
            raise HTTPException(400, "delivered_channels must be a list")
        category = body.get("category")
        event_id = body.get("event_id")
        return await asyncio.to_thread(
            push.notify,
            body=text.strip(),
            title=str(body.get("title") or "Narada"),
            level=level,
            source=str(body.get("source") or "unknown"),
            category=str(category) if category else None,
            url=str(body.get("url") or push.DEFAULT_URL),
            event_id=str(event_id) if event_id else None,
            delivered_channels=[str(c) for c in delivered],
            replay=bool(body.get("replay", False)),
        )

    # ── Web Push (the app leg of notify-suti fan-out) ────────────────
    @app.get("/api/push/vapid-key")
    async def push_vapid_key(tier: str = Depends(require_tier)) -> dict:
        from prana.brain import push

        return {"key": push.ensure_vapid()["public_key"]}

    @app.post("/api/push/subscribe")
    async def push_subscribe(request: Request,
                             tier: str = Depends(require_tier)) -> dict:
        from prana.brain import push

        try:
            sub = await request.json()
        except Exception:
            raise HTTPException(400, "body must be JSON")
        try:
            count = push.subscribe(sub if isinstance(sub, dict) else {})
        except ValueError as exc:
            raise HTTPException(400, str(exc))
        return {"subscriptions": count}

    @app.post("/api/push/unsubscribe")
    async def push_unsubscribe(request: Request,
                               tier: str = Depends(require_tier)) -> dict:
        from prana.brain import push

        body = await request.json()
        endpoint = body.get("endpoint", "") if isinstance(body, dict) else ""
        return {"subscriptions": push.unsubscribe(endpoint)}

    @app.post("/api/push/send")
    async def push_send(request: Request,
                        tier: str = Depends(require_tier)) -> dict:
        # Only the prana tier may broadcast — the app tier subscribes
        # and receives; it must not be able to page every device.
        if tier != "prana":
            raise HTTPException(403, "push send is prana-tier only")
        from prana.brain import push

        try:
            body = await request.json()
        except Exception:
            raise HTTPException(400, "body must be JSON")
        text = body.get("body", "") if isinstance(body, dict) else ""
        if not isinstance(text, str) or not text.strip():
            raise HTTPException(400, "missing body")
        title = body.get("title") or "Narada"
        return await asyncio.to_thread(push.send, str(title), text)

    @app.post("/v1/narada/sessions/{session_id}/cancel")
    async def cancel(session_id: str, tier: str = Depends(require_tier)) -> dict:
        session = pool.peek(session_id, tier)
        if session is None:
            raise HTTPException(404, "no such session")
        was_running = await session.cancel()
        return {"cancelled": was_running}

    @app.post("/v1/chat/completions")
    async def chat_completions(request: Request,
                               tier: str = Depends(require_tier)):
        try:
            body = await request.json()
        except ValueError:
            return _openai_error(400, "body is not valid JSON")
        if not isinstance(body, dict):
            return _openai_error(400, "body must be a JSON object")

        messages = body.get("messages")
        if not isinstance(messages, list) or not messages:
            return _openai_error(400, "messages must be a non-empty list")
        try:
            parsed = _last_user_content(messages)
        except UnsupportedImage as exc:
            return _openai_error(400, str(exc))
        if parsed is None:
            return _openai_error(400, "no user message with content found")
        prompt, images = parsed

        stream = bool(body.get("stream", False))
        narada: Any = body.get("narada") or {}
        if not isinstance(narada, dict):
            return _openai_error(400, "narada extension must be an object")
        session_id = narada.get("session_id")
        request_id = narada.get("request_id")
        completion_id = f"chatcmpl-{uuid.uuid4().hex[:24]}"

        if session_id is None:
            if images:
                return _openai_error(
                    400, "image input requires a narada.session_id session")
            return await _stateless(prompt, messages, stream, completion_id)

        if not valid_session_id(str(session_id)):
            return _openai_error(400, "invalid narada.session_id")
        requested_model = body.get("model")
        if requested_model is not None and requested_model not in MODELS:
            return _openai_error(
                400, f"model must be one of {', '.join(MODELS)}")
        # The pool namespaces by the authenticated tier: one credential
        # can never open another credential's session (spec §1a). The
        # model pins on first creation (plan §T2); a request naming a
        # different one re-pins the session on the same transcript
        # (Suti 2026-09-08: "I can't change the model in the chat") —
        # but only a NEW turn does. A replay or a rejected request_id
        # must never move the pin (review), so the switch waits until
        # the record check below has passed.
        try:
            session = await pool.get_or_create(
                str(session_id), tier, requested_model)
            needs_switch = False
        except ModelConflict:
            session = await pool.get_or_create(str(session_id), tier, None)
            needs_switch = True
        # The model this request means: the one it names, else the pin.
        # Fingerprints bind to it, so a retry that names the old model
        # still replays after a switch.
        effective_model = requested_model or session.model

        # Fingerprint binds the images too — same id + different image
        # must 422 like different text (spec §1a state machine).
        fp_payload = prompt
        if images:
            import hashlib as _hashlib

            fp_payload += "|" + "|".join(
                _hashlib.sha256(i["data"].encode("utf-8")).hexdigest()
                for i in images)
        fp = fingerprint(session.session_id, effective_model, fp_payload)
        if request_id is not None:
            request_id = str(request_id)

        def _known(sess):
            """Replay / 422 / 409 for a request_id already on record;
            None for a fresh one. Synchronous — no await inside."""
            if request_id is None:
                return None
            record = sess.turns.get(request_id)
            if record is None:
                return None
            if record.fingerprint != fp:
                return _openai_error(
                    422, "request_id already used with a different request")
            if record.state not in TERMINAL_STATES:
                return _openai_error(
                    409, "turn with this request_id is in flight",
                    "server_error", headers={"Retry-After": "5"})
            return _replay(record, completion_id, stream, effective_model)

        known = _known(session)
        if known is not None:
            return known
        if needs_switch:
            # Only a turn in flight blocks the switch — 409, the
            # client's "busy, retry" signal. The guard re-runs the
            # record check under the pool lock: a queued request may
            # have completed this request_id while we waited (recheck).
            try:
                session = await pool.switch_model(
                    str(session_id), tier, requested_model,
                    guard=lambda warm: _known(warm or session) is None)
            except SessionBusy:
                return _openai_error(
                    409, "session has an active turn — change the model "
                    "after it finishes", "server_error",
                    headers={"Retry-After": "5"})
            except SwitchRefused:
                pass  # the record check below answers instead
            # The switch awaited: re-run the record check on the new
            # session object before the atomic reserve below.
            known = _known(pool.peek(str(session_id), tier) or session)
            if known is not None:
                return known

        # Reserve + accept in one event-loop tick (no await between the
        # record check above and here) — atomic w.r.t. every other
        # handler, so a same-request_id race can never execute twice.
        try:
            session.reserve()
        except SessionBusy:
            return _openai_error(
                409, "session has an active turn", "server_error",
                headers={"Retry-After": "5"})
        try:
            if request_id is not None:
                session.turns.accept(request_id, fp)  # durable at ACCEPT
            transcript_text = prompt if not images else (
                f"{prompt} [{len(images)} image(s) attached]".strip())
            run = TurnRun()
            run.task = asyncio.create_task(_execute_turn(
                run, session, pool, _content_blocks(prompt, images),
                transcript_text, request_id,
                config.deadline_for(session.model)))
        except BaseException:
            # If durable acceptance (or task creation) fails, the
            # reservation must not outlive it — a leaked reservation
            # 409s the session until restart (recheck P1).
            session.release()
            raise

        if stream:
            return StreamingResponse(
                _tap_stream(run, completion_id, session.model),
                media_type="text/event-stream")
        await run.task
        if run.state == "completed":
            body_out = _completion_body(session.model, run.text, completion_id)
            if run.cards:
                body_out["narada_cards"] = run.cards
            return JSONResponse(body_out)
        return _openai_error(
            500, f"turn {run.state}: {run.error or ''}".strip(),
            "server_error")

    async def _tap_stream(run: TurnRun, completion_id: str, model: str):
        """Stream the turn's events. If the reader disconnects, this
        generator dies but the turn task keeps running to a terminal,
        persisted state — disconnect ≠ cancel."""
        yield _chunk(model, completion_id, {"role": "assistant"})
        while True:
            event = await run.queue.get()
            if event[0] == "delta":
                yield _chunk(model, completion_id, {"content": event[1]})
                continue
            if event[0] == "card":
                # An extension field on an otherwise-empty delta chunk:
                # baseline OpenAI clients ignore it, ours renders it.
                yield _chunk(model, completion_id, {}, card=event[1])
                continue
            if event[0] == "activity":
                yield _chunk(model, completion_id, {}, activity=event[1])
                continue
            _, state, detail = event
            if state == "completed":
                yield _chunk(model, completion_id, {}, finish="stop")
            else:
                yield _stream_error(f"turn {state}: {detail}")
            yield "data: [DONE]\n\n"
            return

    def _replay(record, completion_id: str, stream: bool, model: str):
        """Return a recorded terminal outcome verbatim (spec §1a)."""
        if record.state == "completed":
            cards = list(getattr(record, "cards", []) or [])
            if stream:
                async def _replay_stream():
                    yield _chunk(model, completion_id, {"role": "assistant"})
                    for spec in cards:
                        yield _chunk(model, completion_id, {}, card=spec)
                    yield _chunk(model, completion_id,
                                 {"content": record.result or ""})
                    yield _chunk(model, completion_id, {}, finish="stop")
                    yield "data: [DONE]\n\n"
                return StreamingResponse(_replay_stream(),
                                         media_type="text/event-stream")
            body_out = _completion_body(model, record.result or "", completion_id)
            if cards:
                body_out["narada_cards"] = cards
            return JSONResponse(body_out)
        # failed / cancelled / interrupted: report the recorded state
        # explicitly; the client must send a NEW request_id to rerun.
        return _openai_error(
            409,
            f"turn {record.request_id} ended as {record.state}: "
            f"{record.result or 'no detail'} — send a new request_id to rerun",
            "server_error",
        )

    async def _stateless(prompt: str, messages: list, stream: bool,
                         completion_id: str):
        """No session id: context only from `messages`; ephemeral agent,
        no tools, nothing persisted. Degraded mode by design."""
        context = _render_history(messages)
        backend = backend_factory(
            model=config.model,
            system_append=config.system_append(),
            mcp_servers={},
            max_tool_iterations=config.max_tool_iterations,
            cwd=str(config.sessions_root),
            resume=None,
        )
        await backend.start()

        async def _gen():
            # Same wall-clock bound as session turns — a stuck stateless
            # request must not wedge a semaphore slot forever (P2, diff
            # review 2026-09-05).
            agen = backend.run_turn(context)
            started = time.monotonic()
            try:
                # Deadline covers semaphore queueing too (recheck P2).
                try:
                    await asyncio.wait_for(pool.turn_semaphore.acquire(),
                                           timeout=config.turn_deadline_s)
                except asyncio.TimeoutError:
                    raise TimeoutError(
                        "turn queued behind a full pool for "
                        f"{config.turn_deadline_s:.0f}s — wall-clock bound"
                    ) from None
                try:
                    while True:
                        remaining = config.turn_deadline_s - (
                            time.monotonic() - started)
                        if remaining <= 0:
                            raise TimeoutError(
                                f"turn exceeded {config.turn_deadline_s:.0f}s"
                                " wall-clock bound")
                        try:
                            text = await asyncio.wait_for(
                                agen.__anext__(), timeout=remaining)
                        except StopAsyncIteration:
                            break
                        except asyncio.TimeoutError:
                            raise TimeoutError(
                                f"turn exceeded {config.turn_deadline_s:.0f}s"
                                " wall-clock bound") from None
                        # Stateless is text-only by contract: the SDK's
                        # built-in tools still run without MCP servers,
                        # so card and activity events arrive here too and
                        # are dropped, never serialized as text (review).
                        if not isinstance(text, str):
                            continue
                        yield text
                finally:
                    pool.turn_semaphore.release()
            finally:
                await agen.aclose()
                await backend.close()

        if stream:
            async def _s():
                yield _chunk(config.model, completion_id,
                             {"role": "assistant"})
                try:
                    async for text in _gen():
                        yield _chunk(config.model, completion_id,
                                     {"content": text})
                except TimeoutError as exc:
                    yield _stream_error(str(exc))
                    yield "data: [DONE]\n\n"
                    return
                except Exception:
                    logger.exception("stateless turn failed")
                    yield _stream_error("agent turn failed")
                    yield "data: [DONE]\n\n"
                    return
                yield _chunk(config.model, completion_id, {}, finish="stop")
                yield "data: [DONE]\n\n"
            return StreamingResponse(_s(), media_type="text/event-stream")
        try:
            parts = [t async for t in _gen()]
        except TimeoutError as exc:
            return _openai_error(500, str(exc), "server_error")
        except Exception:
            logger.exception("stateless turn failed")
            return _openai_error(500, "agent turn failed", "server_error")
        return JSONResponse(
            _completion_body(config.model, "".join(parts), completion_id))

    return app
