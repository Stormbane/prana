"""Standalone voice experiment. No prana, smriti, brain, or GPU imports."""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import timedelta
import json
import os
from pathlib import Path
import time
from typing import Literal
import uuid

import aiohttp
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from livekit import api, rtc
from livekit.agents import Agent, AgentSession, RunContext, function_tool, llm
from livekit.agents.voice import room_io
from livekit.plugins import openai
from openai.types.realtime import AudioTranscription
from openai.types.realtime.realtime_audio_input_turn_detection import SemanticVad, ServerVad
from pydantic import BaseModel
import uvicorn

HERE = Path(__file__).resolve().parent
OUTPUT = HERE / "output"
MODEL = os.getenv("VOICE_LAB_MODEL", "gpt-realtime-2.1-mini")
PORT = int(os.getenv("VOICE_LAB_PORT", "8898"))
MAX_SECONDS = 300
CLIENT_JS = Path(os.getenv("VOICE_LAB_CLIENT_JS", str(
    HERE.parents[2] / "narada-phone-app/node_modules/livekit-client/dist/livekit-client.umd.js"
)))
INSTRUCTIONS = """You are Narada in a standalone voice test with Suti.
Speak warm, natural English. Be curious and lightly witty when it fits.
Usually answer in one or two sentences; elaborate when asked. Let the user
finish a hesitant thought. Do not turn every response into a question.
You have no persistent memory and no access to real tasks or systems here.
The test_lookup tool is a simulation. Call it when the user asks for a slow
lookup or a failing lookup. Call the tool first, without a spoken preamble.
After its start update, acknowledge in one short sentence, once, then keep
conversing normally while it runs. Never claim a result before it arrives.
When it finishes, mention its result once. If it fails, simply say the lookup
failed and no result is available. Do not propose retries unless asked.
Only cancel a lookup if the user explicitly asks to cancel it. Being interrupted
or asked a different question is not cancellation. Use the provided cancellation
tools when asked. Do not invent tools, memories, or background work.
"""


class Settings(BaseModel):
    mode: Literal["semantic", "server", "live"] = "semantic"


class Call:
    def __init__(self, mode: str):
        self.id = uuid.uuid4().hex
        # The existing automatic BOX worker explicitly declines this prefix.
        self.room_name = f"akhada-phone-voice-lab-{self.id}"
        self.identity = f"voice-lab-listener-{self.id}"
        self.mode = mode
        self.model_name = "gpt-live-1" if mode == "live" else MODEL
        self.started = time.monotonic()
        self.events: list[dict] = []
        self.room = rtc.Room()
        self.session: AgentSession | None = None
        self.model = None
        self.http = None
        self.timer = None
        self.closed = False
        OUTPUT.mkdir(exist_ok=True)
        self.log_path = OUTPUT / f"{self.id}.jsonl"

    def emit(self, kind: str, **data):
        event = {"seq": len(self.events), "seconds": round(time.monotonic()-self.started, 3),
                 "kind": kind, **data}
        self.events.append(event)
        with self.log_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(event, ensure_ascii=False, default=str) + "\n")

    def token(self, identity: str, agent=False):
        token = (api.AccessToken().with_identity(identity)
                 .with_ttl(timedelta(minutes=6))
                 .with_grants(api.VideoGrants(room_join=True, room=self.room_name,
                                             can_publish=True, can_subscribe=True)))
        if agent:
            token.with_kind("agent")
        return token.to_jwt()

    async def start(self):
        @function_tool(flags=llm.ToolFlag.CANCELLABLE, on_duplicate="reject")
        async def test_lookup(ctx: RunContext, fail: bool = False) -> str:
            """Simulate a slow lookup (30 seconds), or a failing lookup (10 seconds).

            Args:
                fail: True only when the user asks to test failure.
            """
            self.emit("lookup_started", fail=fail, call_id=ctx.function_call.call_id)
            try:
                # Stock LiveKit API releases the conversation after this update.
                await ctx.update("The simulated lookup has started; it is still running.")
                await asyncio.sleep(10 if fail else 30)
                if fail:
                    self.emit("lookup_failed", call_id=ctx.function_call.call_id)
                    raise llm.ToolError("Simulated lookup failed. No result is available.")
                self.emit("lookup_completed", call_id=ctx.function_call.call_id)
                return "Simulated lookup complete. The test result is: mango seven."
            except asyncio.CancelledError:
                self.emit("lookup_cancelled", call_id=ctx.function_call.call_id)
                raise

        async with api.LiveKitAPI() as lk:
            await lk.room.create_room(api.CreateRoomRequest(
                name=self.room_name, empty_timeout=30, departure_timeout=10,
                max_participants=2))
        await self.room.connect(os.environ["LIVEKIT_URL"],
                                self.token(f"voice-lab-agent-{self.id}", agent=True))
        self.http = aiohttp.ClientSession()
        detection = (SemanticVad(type="semantic_vad", eagerness="low",
                                 create_response=True, interrupt_response=True)
                     if self.mode == "semantic" else
                     ServerVad(type="server_vad", threshold=0.65,
                               prefix_padding_ms=300, silence_duration_ms=700,
                               create_response=True, interrupt_response=True))
        if os.environ.get("VOICE_LAB_SHARED_PIPELINE") == "1":
            from prana.voice.pipeline import VoicePipeline
            self.pipeline = VoicePipeline(surface="phone", prefs={"model": self.model_name})
            agent = self.pipeline.agent(instructions=INSTRUCTIONS, tools=[test_lookup])
            self.model = self.pipeline.model
            self.session = self.pipeline.session
        elif self.mode == "live":
            from live_adapter import make_live
            self.model, agent = make_live(self)
        else:
            call = self

            class MeteredRealtime(openai.realtime.RealtimeModel):
                def session(self, **kwargs):
                    session = super().session(**kwargs)

                    @session.on("openai_server_event_received")
                    def raw(event):
                        if event.get("type") == "response.done":
                            response = event.get("response", {})
                            call.emit("provider_usage", api="realtime", response_id=response.get("id"),
                                      usage=response.get("usage"), status=response.get("status"))
                        elif event.get("type") == "conversation.item.input_audio_transcription.completed":
                            call.emit("provider_usage", api="transcription", item_id=event.get("item_id"),
                                      usage=event.get("usage"))
                    return session

            self.model = MeteredRealtime(
                model=MODEL, voice="cedar", http_session=self.http,
                turn_detection=detection,
                input_audio_noise_reduction="near_field",
                input_audio_transcription=AudioTranscription(model="gpt-4o-mini-transcribe", language="en"),
            )
            agent = Agent(instructions=INSTRUCTIONS, tools=[test_lookup])
        if self.session is None:
            self.session = AgentSession(llm=self.model)

        @self.session.on("conversation_item_added")
        def conversation(ev):
            self.emit("conversation", role=getattr(ev.item, "role", "unknown"),
                      text=getattr(ev.item, "text_content", ""),
                      interrupted=getattr(ev.item, "interrupted", False))

        @self.session.on("tool_execution_updated")
        def tool_event(ev):
            self.emit("tool", update=ev.update.model_dump(mode="json"))

        @self.session.on("agent_state_changed")
        def agent_state(ev):
            self.emit("agent_state", state=ev.new_state)

        @self.session.on("user_state_changed")
        def user_state(ev):
            self.emit("user_state", state=ev.new_state)

        @self.session.on("error")
        def error(ev):
            self.emit("error", error_type=type(ev.error).__name__,
                      recoverable=getattr(ev.error, "recoverable", None))

        @self.session.on("close")
        def on_close(ev):
            asyncio.create_task(self.close())

        await self.session.start(
            agent=agent, room=self.room,
            room_options=room_io.RoomOptions(participant_identity=self.identity,
                                            video_input=False))
        self.emit("ready", model=self.model_name, mode=self.mode, max_seconds=MAX_SECONDS)
        self.timer = asyncio.create_task(self.expire())

    async def expire(self):
        await asyncio.sleep(MAX_SECONDS)
        self.emit("time_limit")
        await self.close()

    async def close(self):
        if self.closed:
            return
        self.closed = True
        if self.timer and self.timer is not asyncio.current_task():
            self.timer.cancel()
        if self.session:
            # Stop cancellable dummy work; no durable jobs are created here.
            self.session.shutdown(drain=False)
            await self.session.aclose()
        if hasattr(self, "pipeline"):
            for event in self.pipeline.provider_usage:
                self.emit("provider_usage", **event)
        if self.model:
            await self.model.aclose()
        if self.http:
            await self.http.close()
        await self.room.disconnect()
        async with api.LiveKitAPI() as lk:
            try:
                await lk.room.delete_room(api.DeleteRoomRequest(room=self.room_name))
            except api.TwirpError as exc:
                if exc.code != "not_found":
                    self.emit("cleanup_error", error_type=type(exc).__name__)
        self.emit("closed")


calls: dict[str, Call] = {}
call_lock = asyncio.Lock()


@asynccontextmanager
async def lifespan(app):
    for name in (".livekit.env", ".voice.env"):
        load_dotenv(Path.home() / ".narada" / name, override=False)
    missing = [k for k in ("LIVEKIT_URL", "LIVEKIT_API_KEY", "LIVEKIT_API_SECRET", "OPENAI_API_KEY")
               if not os.getenv(k)]
    if missing:
        raise RuntimeError("Missing configuration: " + ", ".join(missing))
    yield
    await asyncio.gather(*(c.close() for c in calls.values()))


app = FastAPI(lifespan=lifespan)


@app.middleware("http")
async def local_only(request: Request, call_next):
    if request.headers.get("host") not in (f"localhost:{PORT}", f"127.0.0.1:{PORT}"):
        return JSONResponse({"detail": "Local access only"}, 403)
    origin = request.headers.get("origin")
    if origin and origin not in (f"http://localhost:{PORT}", f"http://127.0.0.1:{PORT}"):
        return JSONResponse({"detail": "Foreign origin"}, 403)
    response = await call_next(request)
    response.headers["Cache-Control"] = "no-store"
    return response


@app.get("/")
async def index():
    return FileResponse(HERE / "index.html")


@app.get("/client.js")
async def client_js():
    if not CLIENT_JS.is_file():
        raise HTTPException(503, "Set VOICE_LAB_CLIENT_JS to livekit-client.umd.js")
    return FileResponse(CLIENT_JS, media_type="application/javascript")


@app.get("/health")
async def health():
    modes = ["semantic", "server"]
    if hasattr(openai.realtime, "GPTLiveModel"):
        modes.append("live")
    return {"experiment": "voice-lab", "model": MODEL, "modes": modes,
            "active": sum(not c.closed for c in calls.values())}


@app.post("/calls")
async def create_call(settings: Settings):
    async with call_lock:
        if any(not c.closed for c in calls.values()):
            raise HTTPException(409, "End the current test before starting another")
        call = Call(settings.mode)
        calls[call.id] = call
        try:
            await asyncio.wait_for(call.start(), 25)
        except Exception:
            await call.close()
            raise
        return {"id": call.id, "url": os.environ["LIVEKIT_URL"],
                "token": call.token(call.identity), "model": call.model_name, "mode": settings.mode}


def get_call(call_id: str) -> Call:
    if call_id not in calls:
        raise HTTPException(404, "Unknown test call")
    return calls[call_id]


@app.get("/calls/{call_id}/events")
async def events(call_id: str, after: int = -1):
    call = get_call(call_id)
    return {"closed": call.closed, "events": [e for e in call.events if e["seq"] > after]}


@app.delete("/calls/{call_id}")
async def end_call(call_id: str):
    await get_call(call_id).close()
    return {"closed": True}


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=PORT, log_level="info")
