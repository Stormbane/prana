"""Shared selection, real provider construction and preserved tool boundaries."""

import pytest
import subprocess
import sys
from livekit.agents import function_tool
from livekit.plugins.openai.realtime import GPTLiveModel, RealtimeModel

from prana.voice.pipeline import VoicePipeline
from prana.voice.prefs import selected_model, update_prefs


@pytest.mark.parametrize("worker", ["worker", "phone_worker"])
def test_fresh_worker_registers_plugin_before_job_thread(worker):
    # A fresh process matters: this pytest process already imported the plugin.
    code = f"""
import prana.voice.{worker}
from concurrent.futures import ThreadPoolExecutor
def job():
    from prana.voice.pipeline import VoicePipeline
    if {worker!r} == 'phone_worker':
        try:
            from livekit.plugins.google.realtime import realtime_api  # the Gemini talker's first import
        except ImportError:
            pass  # no Gemini in this environment
    return VoicePipeline.__name__
with ThreadPoolExecutor(max_workers=1) as pool:
    assert pool.submit(job).result() == 'VoicePipeline'
"""
    subprocess.run([sys.executable, "-c", code], check=True, capture_output=True,
                   text=True, timeout=30)


def test_box_never_auto_joins_either_phone_surface():
    from prana.voice.worker import is_phone_room

    assert is_phone_room("narada-phone-current-app")
    assert is_phone_room("akhada-phone-legacy-app")
    assert not is_phone_room("narada-body")


@pytest.mark.asyncio
async def test_both_surfaces_read_the_same_saved_model(tmp_path, monkeypatch):
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    monkeypatch.delenv("NARADA_VOICE_MODEL", raising=False)
    assert selected_model() == "gpt-live-1"
    for model in ("gpt-realtime-2.1-mini", "gpt-live-1"):
        update_prefs({"model": model})
        assert VoicePipeline(surface="phone").model_name == model
        assert VoicePipeline(surface="box").model_name == model
    with pytest.raises(ValueError, match="Unsupported"):
        selected_model({"model": "invented"})


@pytest.mark.asyncio
async def test_saved_voice_follows_both_surfaces_and_survives_model_switch(tmp_path, monkeypatch):
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    monkeypatch.setenv("NARADA_LIVE_VOICE", "marin")
    update_prefs({"live_voice": "vesper", "realtime_voice": "cedar"})
    for model, voice in [("gpt-live-1", "vesper"), ("gpt-realtime-2.1-mini", "cedar"),
                         ("gpt-live-1", "vesper")]:
        update_prefs({"model": model})
        for surface in ["phone", "box"]:
            assert VoicePipeline(surface=surface).voice == voice


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "model,provider", [("gpt-live-1", GPTLiveModel), ("gpt-realtime-2.1-mini", RealtimeModel)]
)
async def test_provider_preserves_only_supplied_tools(monkeypatch, model, provider):
    monkeypatch.setenv("OPENAI_API_KEY", "test-not-a-key")

    @function_tool()
    async def allowed_read() -> str:
        """Read a harmless fixture."""
        return "read"

    pipeline = VoicePipeline(surface="box", prefs={"model": model})
    agent = pipeline.agent(instructions="PRIVATE_CONTEXT_SENTINEL", tools=[allowed_read])
    assert isinstance(pipeline.model, provider)
    assert [tool.id for tool in agent.tools] == [allowed_read.id]
    assert pipeline.info()["model"] == model
    if model == "gpt-live-1":
        # Private context is available to the admitted backend, not duplicated
        # into the short speaking policy; no tools are added by the pipeline.
        assert "PRIVATE_CONTEXT_SENTINEL" not in agent.instructions
        assert pipeline.model._opts.responses["instructions"] == "PRIVATE_CONTEXT_SENTINEL"
        assert pipeline.model._opts.delegation == "responses"
    await pipeline.model.aclose()


@pytest.mark.asyncio
async def test_usage_does_not_include_transcripts(caplog):
    pipeline = VoicePipeline(surface="phone", prefs={"model": "gpt-live-1"})
    pipeline._record({"type": "session.input_transcript.delta", "delta": "private words"})
    pipeline._record({"type": "session.closed", "usage": {"seconds": 82}})
    assert pipeline.provider_usage == [
        {"type": "session.closed", "usage": {"seconds": 82}, "api": "live"}
    ]
    assert "private words" not in caplog.text


@pytest.mark.asyncio
async def test_self_description_uses_the_actual_surface_and_provider(tmp_path):
    from prana.sessions.escalate import ProposalQueue
    from prana.sessions.service import ServiceClient
    from prana.voice.tools import build_voice_tools

    pipeline = VoicePipeline(surface="phone", prefs={"model": "gpt-live-1"})
    tools = build_voice_tools(
        client=ServiceClient(port=1, token="unused"),
        proposals=ProposalQueue(tmp_path / "proposals.db"),
        voice_info=pipeline.info(),
    )
    result = await next(tool for tool in tools if tool.id == "about_myself")()
    assert result["brain_model"] == "gpt-live-1"
    assert "GPT-Live" in result["speech_is"]
    assert result["tool_backend_model"] == "gpt-5.6-luna"
    assert result["body"] == "Narada app on the phone"
