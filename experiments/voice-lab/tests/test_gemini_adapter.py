"""Adapter wiring against the pinned plugin (livekit-plugins-google 1.8.10). No network."""
import asyncio
import inspect
import json

import pytest

pytest.importorskip("livekit.plugins.google")

from google.genai import types  # noqa: E402
from livekit import rtc  # noqa: E402
from livekit.plugins.google.realtime import realtime_api  # noqa: E402

import gemini_adapter  # noqa: E402
from gemini_adapter import BudgetRefused, GeminiTalker, MeteredModel, MeteredSession  # noqa: E402


class FakeCall:
    def __init__(self):
        self.id = "test-call"
        self.events = []
        self.closed = False
        self.reasons = []

    def emit(self, kind, **data):
        self.events.append({"kind": kind, **data})

    async def close(self, reason="ended"):
        if not self.closed:
            self.closed = True
            self.reasons.append(reason)


@pytest.fixture(autouse=True)
def _ledger_in_tmp(tmp_path, monkeypatch):
    """No test may touch the real trial ledger in ~/.narada."""
    monkeypatch.setattr(gemini_adapter, "LEDGER_PATH", tmp_path / "gemini-ledger.json")


@pytest.fixture
def out(tmp_path, monkeypatch):
    monkeypatch.setattr(gemini_adapter, "OUTPUT", tmp_path)
    return tmp_path


def voiced(seconds=0.02, amplitude=3000):
    n = int(24000 * seconds)
    data = (amplitude.to_bytes(2, "little", signed=True)) * n
    return rtc.AudioFrame(data, 24000, 1, n)


def test_plugin_still_has_the_hooks_we_override():
    cls = realtime_api.RealtimeSession
    assert list(inspect.signature(cls.push_audio).parameters) == ["self", "frame"]
    assert list(inspect.signature(cls._handle_server_content).parameters) == ["self", "server_content"]
    assert list(inspect.signature(cls._handle_usage_metadata).parameters) == ["self", "usage_metadata"]
    assert "gemini-3.8-live" in realtime_api.KNOWN_GEMINI_API_MODELS


def test_model_config_for_38_live(out, monkeypatch):
    monkeypatch.setenv("GOOGLE_API_KEY", "test-not-a-key")
    talker = GeminiTalker(FakeCall())
    model = talker.model()
    opts = model._opts
    assert opts.model == "gemini-3.8-live" and opts.voice == "Umbriel"
    assert opts.max_output_tokens == gemini_adapter.MAX_OUTPUT_TOKENS
    assert opts.context_window_compression.trigger_tokens == gemini_adapter.COMPRESSION_TRIGGER_TOKENS
    # 3.8 rejects proactive_audio=false and dropped affective dialog: send neither.
    assert not realtime_api.is_given(opts.proactivity)
    assert not realtime_api.is_given(opts.enable_affective_dialog)


def test_missing_key_fails_before_any_connection(out, monkeypatch):
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    with pytest.raises(RuntimeError, match="GOOGLE_API_KEY"):
        GeminiTalker(FakeCall()).model()


def test_tiny_cap_refuses_before_connecting(out, monkeypatch):
    monkeypatch.setenv("GOOGLE_API_KEY", "test-not-a-key")
    talker = GeminiTalker(FakeCall(), session_cap_usd=0.001)
    model = talker.model()
    with pytest.raises(BudgetRefused):
        talker.open()
    with pytest.raises(BudgetRefused):
        model.session()                     # no provider session is ever created


def test_overrides_can_only_lower_the_server_limits(out):
    talker = GeminiTalker(FakeCall(), session_cap_usd=99, idle_seconds=9999, max_seconds=9999)
    assert talker.budget.pots[0].cap_usd == gemini_adapter.SESSION_CAP_USD
    assert talker.idle.timeout == gemini_adapter.IDLE_SECONDS
    assert talker.max_seconds == gemini_adapter.MAX_SECONDS <= 540


def test_exhausting_the_cap_mid_session_stops_forwarding_audio(out, monkeypatch):
    forwarded = []
    monkeypatch.setattr(realtime_api.RealtimeSession, "push_audio",
                        lambda self, frame: forwarded.append(frame))
    monkeypatch.setattr(realtime_api.RealtimeSession, "_handle_usage_metadata",
                        lambda self, um: None)

    async def scenario():
        call = FakeCall()
        talker = GeminiTalker(call, session_cap_usd=0.012)
        talker.open()
        talker.live = True                   # as after attach()
        sess = MeteredSession.__new__(MeteredSession)   # no connect task
        sess.talker = talker
        sess._current_generation = None
        for _ in range(50):
            sess.push_audio(voiced())
        assert len(forwarded) == 50
        # The provider reports the first turn; the next turn no longer fits.
        sess._handle_usage_metadata(types.UsageMetadata(
            prompt_token_count=900, response_token_count=300,
            prompt_tokens_details=[types.ModalityTokenCount(modality=types.MediaModality.TEXT, token_count=800),
                                   types.ModalityTokenCount(modality=types.MediaModality.AUDIO, token_count=100)],
            response_tokens_details=[types.ModalityTokenCount(modality=types.MediaModality.AUDIO, token_count=300)]))
        for _ in range(50):
            sess.push_audio(voiced())
        await asyncio.sleep(0.05)            # let the deferred close run
        return call, talker

    call, talker = asyncio.run(scenario())
    assert len(forwarded) == 50              # nothing after the cap
    assert talker.guard.stopped == "cap"
    assert call.reasons == ["cap"]
    usage = next(e for e in call.events if e["kind"] == "provider_usage")
    assert usage["usd"] == pytest.approx((800 * 0.75 + 100 * 3 + 300 * 12) / 1e6)
    assert [e["kind"] for e in call.events].count("cap_reached") == 1


def test_idle_timeout_closes_the_call(out, monkeypatch):
    monkeypatch.setattr(gemini_adapter, "IDLE_SECONDS", 0.3)

    async def scenario():
        call = FakeCall()
        talker = GeminiTalker(call, idle_seconds=0.3)
        talker.live = True
        talker._spawn(talker._watch_idle())
        await asyncio.sleep(0.15)
        talker.admit_input(voiced())         # speech resets the clock
        await asyncio.sleep(0.25)
        assert not call.closed
        await asyncio.sleep(0.6)
        return call

    call = asyncio.run(scenario())
    assert call.reasons == ["idle"]
    assert any(e["kind"] == "idle_timeout" for e in call.events)


def test_idle_waits_while_narada_is_still_speaking(out, monkeypatch):
    monkeypatch.setattr(gemini_adapter, "IDLE_SECONDS", 0.3)

    async def scenario():
        call = FakeCall()
        talker = GeminiTalker(call, idle_seconds=0.3)
        talker.live = True
        talker.speaking = True               # a long reply still playing out
        talker._spawn(talker._watch_idle())
        await asyncio.sleep(0.8)
        assert not call.closed
        talker.speaking = False
        await asyncio.sleep(0.8)
        return call

    assert asyncio.run(scenario()).reasons == ["idle"]


def test_silence_does_not_count_as_activity(out):
    talker = GeminiTalker(FakeCall())
    before = talker.idle.last
    talker.admit_input(voiced(amplitude=0))
    assert talker.idle.last == before


def test_transcript_events_and_file_for_the_sage(out):
    async def scenario():
        call = FakeCall()
        talker = GeminiTalker(call)
        talker.open()
        talker.transcript.user("Hello")
        sc = types.LiveServerContent(output_transcription=types.Transcription(text="Hello, Suti. Long story..."))
        talker.on_server_content("g1", sc)
        talker.transcript.played("g1", "Hello, Suti.", interrupted=True)
        talker.on_server_content("g2", types.LiveServerContent(
            output_transcription=types.Transcription(text="Never heard.")))
        assert talker.inject_context("a whisper") is False
        await talker.finish(None, "ended")
        return call

    call = asyncio.run(scenario())
    kinds = [e["kind"] for e in call.events]
    assert "sage_inject_ignored" in kinds and "talker_summary" in kinds
    emitted = [e for e in call.events if e["kind"] == "transcript_assistant"]
    assert "seq" not in emitted[0] and emitted[0]["turn"] == 1
    data = json.loads((out / "talker-test-call.transcript.json").read_text(encoding="utf-8"))
    assert data["stage"] == 1 and data["reason"] == "ended"
    assert data["heard"] == [{"role": "user", "text": "Hello", "interrupted": False},
                             {"role": "assistant", "text": "Hello, Suti.", "interrupted": True}]
    states = [(e["gen_id"], e["state"]) for e in data["entries"] if e["role"] == "assistant"]
    assert states == [("g1", "interrupted"), ("g2", "unplayed")]
    assert json.loads((out / "gemini-ledger.json").read_text())["sessions"][0]["call_id"] == "test-call"


def test_latency_event_on_first_model_audio(out):
    call = FakeCall()
    talker = GeminiTalker(call)
    talker.admit_input(voiced())
    part = types.Part(inline_data=types.Blob(data=bytes(960), mime_type="audio/pcm"))
    talker.on_server_content("g1", types.LiveServerContent(model_turn=types.Content(parts=[part])))
    talker.on_server_content("g1", types.LiveServerContent(model_turn=types.Content(parts=[part])))
    samples = [e for e in call.events if e["kind"] == "latency"]
    assert len(samples) == 1 and samples[0]["gen_id"] == "g1"
    assert samples[0]["eos_to_first_model_audio_ms"] >= 0


def test_cost_py_reprices_a_gemini_report():
    import cost
    report = {"mode": "gemini", "model": "gemini-3.8-live", "events": [
        {"kind": "provider_usage", "api": "gemini-live",
         "usage": {"prompt_token_count": 1000, "response_token_count": 500,
                   "prompt_tokens_details": [{"modality": "AUDIO", "token_count": 1000}],
                   "response_tokens_details": [{"modality": "AUDIO", "token_count": 500}]}}]}
    assert cost.estimate(report)["voice_usd"] == pytest.approx((1000 * 3 + 500 * 12) / 1e6)


def test_lab_offers_gemini_only_with_a_key(monkeypatch):
    import lab
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    assert lab.available_modes() == []
    monkeypatch.setenv("GEMINI_API_KEY", "test-not-a-key")
    assert lab.available_modes() == ["gemini"]
    with pytest.raises(Exception):
        lab.Settings(mode="gemini", session_cap_usd=-1)


# --- the money holes (Codex review, 2026-11-04) ------------------------------------

def _usage(out_audio=100):
    return {"prompt_token_count": 800, "response_token_count": out_audio,
            "prompt_tokens_details": [{"modality": "TEXT", "token_count": 800}],
            "response_tokens_details": [{"modality": "AUDIO", "token_count": out_audio}]}


def test_a_corrupt_ledger_refuses_before_any_connection(out):
    gemini_adapter.LEDGER_PATH.write_text('{"spent_usd": 4.', encoding="utf-8")
    with pytest.raises(BudgetRefused, match="corrupt"):
        GeminiTalker(FakeCall())


def test_each_usage_report_reaches_the_shared_ledger_as_it_arrives(out):
    call = FakeCall()
    talker = GeminiTalker(call)
    talker.open()
    talker.on_usage("g1", _usage())
    first = gemini_adapter.Ledger(gemini_adapter.LEDGER_PATH).spent()
    assert first == pytest.approx(talker.guard.actual_usd) and first > 0
    talker.on_usage("g2", _usage())
    assert gemini_adapter.Ledger(gemini_adapter.LEDGER_PATH).spent() == pytest.approx(talker.guard.actual_usd)


def test_another_process_spend_counts_against_the_trial_cap(out):
    talker = GeminiTalker(FakeCall())
    talker.open()
    gemini_adapter.Ledger(gemini_adapter.LEDGER_PATH).add("smoke-test", 4.99)   # elsewhere
    talker.on_usage("g1", _usage())
    assert talker.trial.spent_usd > 4.99
    assert talker.guard.stopped == "cap"           # the next turn no longer fits the trial


def test_a_reply_cut_off_by_hanging_up_is_recorded(out):
    async def scenario():
        call = FakeCall()
        talker = GeminiTalker(call)
        talker.open()
        talker.on_server_content("g1", types.LiveServerContent(
            output_transcription=types.Transcription(text="Once upon a time")))
        await talker.finish(None, "ended")           # no usage report ever came
        return talker

    talker = asyncio.run(scenario())
    owed = talker.guard.charged_at_close
    assert owed > 0
    assert gemini_adapter.Ledger(gemini_adapter.LEDGER_PATH).spent() == pytest.approx(owed)


def test_a_ledger_that_cannot_be_written_stops_the_call(out, monkeypatch):
    call = FakeCall()
    talker = GeminiTalker(call)
    talker.open()

    def broken(*a, **k):
        raise gemini_adapter.LedgerError("locked")
    monkeypatch.setattr(talker.ledger, "add", broken)
    talker.on_usage("g1", _usage())
    assert talker.guard.stopped == "ledger"
    assert any(e["kind"] == "ledger_write_failed" for e in call.events)
