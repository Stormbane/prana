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
import time
import uuid
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse

from prana.brain.config import BrainConfig
from prana.brain.session import (
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
           finish: str | None = None) -> str:
    payload = {
        "id": completion_id,
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "model": model,
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
    }
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
            session.turns.transition(request_id, state, result)
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


def create_app(config: BrainConfig, backend_factory) -> FastAPI:
    from contextlib import asynccontextmanager

    pool = SessionPool(config, backend_factory)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        pool.start_reaper()
        yield
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
            "data": [{"id": config.model, "object": "model",
                      "created": 0, "owned_by": "narada"}],
        }

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
        # The pool namespaces by the authenticated tier: one credential
        # can never open another credential's session (spec §1a).
        session = await pool.get_or_create(str(session_id), tier)

        # Fingerprint binds the images too — same id + different image
        # must 422 like different text (spec §1a state machine).
        fp_payload = prompt
        if images:
            import hashlib as _hashlib

            fp_payload += "|" + "|".join(
                _hashlib.sha256(i["data"].encode("utf-8")).hexdigest()
                for i in images)
        fp = fingerprint(session.session_id, config.model, fp_payload)
        if request_id is not None:
            request_id = str(request_id)
            record = session.turns.get(request_id)
            if record is not None:
                if record.fingerprint != fp:
                    return _openai_error(
                        422, "request_id already used with a different request")
                if record.state not in TERMINAL_STATES:
                    return _openai_error(
                        409, "turn with this request_id is in flight",
                        "server_error", headers={"Retry-After": "5"})
                return _replay(record, completion_id, stream)

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
                transcript_text, request_id, config.turn_deadline_s))
        except BaseException:
            # If durable acceptance (or task creation) fails, the
            # reservation must not outlive it — a leaked reservation
            # 409s the session until restart (recheck P1).
            session.release()
            raise

        if stream:
            return StreamingResponse(
                _tap_stream(run, completion_id),
                media_type="text/event-stream")
        await run.task
        if run.state == "completed":
            return JSONResponse(
                _completion_body(config.model, run.text, completion_id))
        return _openai_error(
            500, f"turn {run.state}: {run.error or ''}".strip(),
            "server_error")

    async def _tap_stream(run: TurnRun, completion_id: str):
        """Stream the turn's events. If the reader disconnects, this
        generator dies but the turn task keeps running to a terminal,
        persisted state — disconnect ≠ cancel."""
        yield _chunk(config.model, completion_id, {"role": "assistant"})
        while True:
            event = await run.queue.get()
            if event[0] == "delta":
                yield _chunk(config.model, completion_id,
                             {"content": event[1]})
                continue
            _, state, detail = event
            if state == "completed":
                yield _chunk(config.model, completion_id, {}, finish="stop")
            else:
                yield _stream_error(f"turn {state}: {detail}")
            yield "data: [DONE]\n\n"
            return

    def _replay(record, completion_id: str, stream: bool):
        """Return a recorded terminal outcome verbatim (spec §1a)."""
        if record.state == "completed":
            if stream:
                async def _replay_stream():
                    yield _chunk(config.model, completion_id,
                                 {"role": "assistant"})
                    yield _chunk(config.model, completion_id,
                                 {"content": record.result or ""})
                    yield _chunk(config.model, completion_id, {},
                                 finish="stop")
                    yield "data: [DONE]\n\n"
                return StreamingResponse(_replay_stream(),
                                         media_type="text/event-stream")
            return JSONResponse(
                _completion_body(config.model, record.result or "",
                                 completion_id))
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
