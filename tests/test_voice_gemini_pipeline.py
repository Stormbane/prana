"""The Gemini talker inside the shared voice pipeline (presence plan W5a).
No network: the model is built, never connected."""
import logging

import pytest

pytest.importorskip("livekit.plugins.google")

from livekit.agents import function_tool  # noqa: E402

from prana.voice import faults  # noqa: E402
from prana.voice.gemini import adapter  # noqa: E402
from prana.voice.pipeline import GeminiCall, VoicePipeline  # noqa: E402


@pytest.fixture(autouse=True)
def _private_paths(tmp_path, monkeypatch):
    """No test touches the real ledger, talker folder or voice prefs."""
    monkeypatch.setattr(adapter, "LEDGER_PATH", tmp_path / "ledger.json")
    monkeypatch.setattr(adapter, "OUTPUT", tmp_path)
    monkeypatch.setitem(adapter._UNRECORDED, "usd", 0.0)
    monkeypatch.setenv("GOOGLE_API_KEY", "test-not-a-key")


def _call():
    ended = []
    return GeminiCall("room-1", ended.append), ended


@function_tool()
async def allowed_read() -> str:
    """Read a harmless fixture."""
    return "read"


@pytest.mark.asyncio
async def test_gemini_talker_gets_narada_instructions_tools_and_the_chosen_voice():
    pipeline = VoicePipeline(surface="phone", prefs={"model": "gemini-3.8-live", "gemini_voice": "Achird"})
    call, _ = _call()
    agent = pipeline.agent(instructions="PRIVATE_CONTEXT_SENTINEL", tools=[allowed_read], call=call)
    assert isinstance(pipeline.model, adapter.MeteredModel)
    assert [tool.id for tool in agent.tools] == [allowed_read.id]
    assert pipeline.info()["backend"] == "gemini"
    assert pipeline.talker.voice == "Achird"
    # The guard prices the real context, not the lab's test brief.
    assert "PRIVATE_CONTEXT_SENTINEL" in pipeline.talker.instructions
    assert "PRIVATE_CONTEXT_SENTINEL" in agent.instructions
    await pipeline.model.aclose()


@pytest.mark.asyncio
async def test_gemini_needs_the_call_it_may_end():
    pipeline = VoicePipeline(surface="phone", prefs={"model": "gemini-3.8-live"})
    with pytest.raises(RuntimeError, match="needs the call"):
        pipeline.agent(instructions="x", tools=[])


@pytest.mark.asyncio
async def test_the_box_keeps_an_openai_model_until_v_f():
    pipeline = VoicePipeline(surface="box", prefs={"model": "gemini-3.8-live"})
    assert not pipeline.is_gemini
    assert pipeline.model_name == "gpt-realtime-mini"


@pytest.mark.asyncio
async def test_switching_back_is_one_step(monkeypatch):
    """V-E gate 8: choosing an OpenAI model again is the whole rollback."""
    from livekit.plugins.openai.realtime import RealtimeModel

    monkeypatch.setenv("OPENAI_API_KEY", "test-not-a-key")
    call, _ = _call()
    gemini = VoicePipeline(surface="phone", prefs={"model": "gemini-3.8-live"})
    gemini.agent(instructions="x", tools=[], call=call)
    await gemini.model.aclose()
    openai = VoicePipeline(surface="phone", prefs={"model": "gpt-realtime-mini"})
    openai.agent(instructions="x", tools=[], call=call)
    assert isinstance(openai.model, RealtimeModel) and openai.talker is None
    await openai.model.aclose()


@pytest.mark.asyncio
async def test_an_exhausted_budget_refuses_before_any_connection_and_says_so():
    adapter.Ledger(adapter.LEDGER_PATH).add("earlier", adapter.TRIAL_CAP_USD)
    pipeline = VoicePipeline(surface="phone", prefs={"model": "gemini-3.8-live"})
    call, _ = _call()
    with pytest.raises(adapter.BudgetRefused) as refused:
        pipeline.agent(instructions="x", tools=[], call=call)
    assert pipeline.model is None  # nothing was built, nothing connected
    fault = faults.classify(refused.value, "gemini")
    assert fault.code == "budget" and fault.fatal
    assert "spending cap" in fault.text


def test_gemini_faults_are_worded_for_gemini():
    assert "Gemini key" in faults.classify("API key not valid. Please pass a valid API key.", "gemini").text
    assert "OpenAI" in faults.classify("invalid_api_key").text


@pytest.mark.asyncio
async def test_call_events_are_logged_without_words(caplog):
    call, ended = _call()
    with caplog.at_level(logging.INFO, logger="narada-voice"):
        call.emit("played", text="private words", turn=3)
        # The transcript's own events carry the words in other fields too
        # (Codex review: `generated`); nothing but labels and numbers passes.
        call.emit("played", seq=4, generated="private words", heard="private words",
                  interrupted=True, entry={"generated": "private words", "seq": 4})
        call.emit("talker_summary", heard="private words", cost_usd=0.01, reason="cap",
                  entries=[{"text": "private words"}], budget={"session": {"cap_usd": 0.5}})
    assert "private words" not in caplog.text
    assert "cost_usd" in caplog.text and '"reason": "cap"' in caplog.text
    assert '"cap_usd": 0.5' in caplog.text
    await call.close("cap")
    await call.close("idle")
    assert ended == ["cap"] and call.reason == "cap"


def test_the_talkers_limit_bounds_the_phone_call():
    """Codex review: the talker's duration limit was never applied."""
    from types import SimpleNamespace

    from prana.voice.phone_worker import session_limit

    assert session_limit(600.0, None) == 600.0
    assert session_limit(600.0, SimpleNamespace(max_seconds=540.0)) == 540.0
    assert session_limit(300.0, SimpleNamespace(max_seconds=540.0)) == 300.0


def test_the_phone_needs_the_gemini_key_when_gemini_is_chosen(tmp_path, monkeypatch):
    import json
    from pathlib import Path

    from prana.voice import worker

    (tmp_path / ".narada" / "brain").mkdir(parents=True)
    (tmp_path / ".narada" / "brain" / "voice-prefs.json").write_text(
        json.dumps({"model": "gemini-3.8-live"}), encoding="utf-8")
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    for k in ("LIVEKIT_URL", "LIVEKIT_API_KEY", "LIVEKIT_API_SECRET", "OPENAI_API_KEY"):
        monkeypatch.setenv(k, "test-value")
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    monkeypatch.delenv("NARADA_VOICE_BACKEND", raising=False)
    with pytest.raises(SystemExit, match="GOOGLE_API_KEY"):
        worker._check_env(surface="phone")
    worker._check_env(surface="box")  # the BOX stays on OpenAI
    monkeypatch.setenv("GOOGLE_API_KEY", "test-not-a-key")
    worker._check_env(surface="phone")
