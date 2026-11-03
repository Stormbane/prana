"""GPT-Live adapter with a deterministic dummy backend, not another model."""
import asyncio
import re

from livekit.agents import Agent
from livekit.plugins.openai.realtime import GPTLiveModel


INSTRUCTIONS = """You are Narada, talking with Suti in a voice experiment.
Speak warm, natural English, with a little dry wit when it fits. Be concise
unless invited to tell a story. You have no persistent memory in this test.
Backchannel policy: Offer occasional brief listening sounds when helpful.
Interruption policy: When interrupted, pause your answer and hear the caller.
Delegation policy:
Backend tools:
- Simulated lookups: start a slow lookup, start a failing lookup, or cancel one.
Delegate to the backend when:
- Suti requests one of those lookup operations, including cancellation.
Do not delegate to the backend when:
- Greeting, chatting, answering simple arithmetic, or telling a story.
- You need clarification about the requested operation.
Delegate before claiming any lookup action or result. Let conversation continue
while it runs; announce its result or failure when the backend reports it.
"""


def make_live(call):
    class MeteredLive(GPTLiveModel):
        def session(self):
            session = super().session()

            @session.on("openai_server_event_received")
            def raw(event):
                kind = event.get("type", "")
                if kind in ("session.usage.updated", "session.closed"):
                    call.emit("provider_usage", api="live", event_type=kind,
                              usage=event.get("usage", {}), reason=event.get("reason"))
                elif kind == "session.started":
                    call.emit("provider_started", api="live")
                elif kind in ("session.input_transcript.delta", "session.output_transcript.delta"):
                    call.emit("transcript_delta", role="user" if "input" in kind else "assistant",
                              text=event.get("delta", ""), start_ms=event.get("start_ms"),
                              end_ms=event.get("end_ms"))
            return session

    class LiveAgent(Agent):
        def __init__(self):
            super().__init__(instructions=INSTRUCTIONS)
            self.work = None
            self.work_id = None
            self.handlers = set()
            self.seen = set()

        async def on_enter(self):
            self.duplex_session.on("delegation_created", self.delegated)

        def delegated(self, delegation):
            if delegation.id in self.seen:
                return
            self.seen.add(delegation.id)
            task = asyncio.create_task(self.handle(delegation))
            self.handlers.add(task)
            task.add_done_callback(self.handlers.discard)

        def say_result(self, message, delegation_id):
            self.duplex_session.append_commentary(message, delegation_id=delegation_id)

        async def handle(self, delegation):
            # This deliberately narrow backend recognizes only the lab commands.
            # Unknown requests fail visibly instead of invoking any real system.
            pending = delegation.pending_transcript
            user_items = [getattr(item, "text_content", "") for item in self.chat_ctx.items
                          if getattr(item, "role", None) == "user"]
            request = (pending or (user_items[-1] if user_items else "")).lower()
            call.emit("delegation", call_id=delegation.id, request=request)
            if re.search(r"\bcancel\b|\bstop (?:that|the|my|a|background).*lookup", request):
                if self.work and not self.work.done():
                    self.work.cancel()
                    await asyncio.gather(self.work, return_exceptions=True)
                    self.say_result("The lookup was cancelled. It will not produce a result.", delegation.id)
                else:
                    self.say_result("There is no running lookup to cancel.", delegation.id)
            elif "lookup" in request or "look up" in request:
                if self.work and not self.work.done():
                    self.say_result("The existing simulated lookup is still running; no new lookup was started.", delegation.id)
                    return
                fail = "fail" in request
                self.work_id = delegation.id
                self.work = asyncio.create_task(self.lookup(delegation.id, fail))
            else:
                self.say_result("This test backend only supports starting slow or failing lookups and cancelling them. No action was taken.", delegation.id)

        async def lookup(self, call_id, fail):
            call.emit("lookup_started", fail=fail, call_id=call_id)
            self.say_result("The simulated lookup has started. It is still running.", call_id)
            try:
                await asyncio.sleep(10 if fail else 30)
                if fail:
                    call.emit("lookup_failed", call_id=call_id)
                    self.say_result("The lookup failed. No result is available.", call_id)
                else:
                    call.emit("lookup_completed", call_id=call_id)
                    self.say_result("The simulated lookup completed. The result is mango seven.", call_id)
            except asyncio.CancelledError:
                call.emit("lookup_cancelled", call_id=call_id)
                raise

        async def on_exit(self):
            pending = list(self.handlers) + ([self.work] if self.work else [])
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)

    return MeteredLive(model="gpt-live-1", voice="marin", delegation="client",
                       http_session=call.http), LiveAgent()
