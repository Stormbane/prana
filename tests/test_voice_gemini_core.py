"""Unit tests for the Gemini talker core: no network, no LiveKit server, no spend."""
from array import array
import json

import pytest

from prana.voice.gemini.core import (
    INTERRUPTED, PLAYED, UNPLAYED, Budget, CostGuard, IdleWatchdog, LatencyMeter, Ledger, LedgerError, Pot,
    Rates, Transcript, estimate_tokens, instructions, pcm16_rms, price_usage, rates_for,
)

RATES = rates_for("gemini-3.8-live")


class Clock:
    def __init__(self, t=100.0):
        self.t = t

    def __call__(self):
        return self.t


def usage(prompt_audio=0, prompt_text=0, out_audio=0, out_text=0, thoughts=0):
    return {
        "prompt_token_count": prompt_audio + prompt_text,
        "prompt_tokens_details": [{"modality": "AUDIO", "token_count": prompt_audio},
                                  {"modality": "TEXT", "token_count": prompt_text}],
        "response_token_count": out_audio + out_text,
        "response_tokens_details": [{"modality": "AUDIO", "token_count": out_audio},
                                    {"modality": "TEXT", "token_count": out_text}],
        "thoughts_token_count": thoughts,
    }


# --- prices ---------------------------------------------------------------------

def test_checked_rates_for_38_live():
    assert RATES == Rates(text_in=0.75, audio_in=3.00, text_out=4.50, audio_out=12.00)
    with pytest.raises(ValueError):
        rates_for("gemini-unpriced-live")


def test_price_usage_by_modality_and_thinking():
    usd = price_usage(usage(prompt_audio=1_000_000, prompt_text=1_000_000,
                            out_audio=1_000_000, out_text=1_000_000, thoughts=1_000_000), RATES)
    assert usd == pytest.approx(3.00 + 0.75 + 12.00 + 4.50 + 4.50)


def test_unlabelled_tokens_pay_the_higher_rate():
    assert price_usage({"prompt_token_count": 1_000_000, "response_token_count": 1_000_000},
                       RATES) == pytest.approx(3.00 + 12.00)


def test_enum_style_modality_names_are_understood():
    u = {"prompt_token_count": 1_000_000,
         "prompt_tokens_details": [{"modality": "MediaModality.TEXT", "token_count": 1_000_000}]}
    assert price_usage(u, RATES) == pytest.approx(0.75)


# --- budget ---------------------------------------------------------------------

def test_reservation_needs_headroom_in_every_pot():
    budget = Budget([Pot("session", 1.0), Pot("trial", 5.0, spent_usd=4.5)])
    assert budget.reserve(0.6, "x") is None          # the trial pot refuses
    r = budget.reserve(0.4, "x")
    assert r and budget.pots[0].reserved_usd == pytest.approx(0.4)
    budget.settle(r, 0.1)
    assert budget.pots[0].reserved_usd == 0 and budget.pots[1].spent_usd == pytest.approx(4.6)


def test_actual_spend_is_recorded_even_past_the_cap():
    budget = Budget([Pot("session", 0.10)])
    budget.charge(0.25)
    assert budget.exhausted and budget.pots[0].spent_usd == pytest.approx(0.25)


def test_ledger_accumulates_across_sessions(tmp_path):
    ledger = Ledger(tmp_path / "ledger.json")
    assert ledger.spent() == 0
    ledger.add("a", 0.12)
    ledger.add("b", 0.03)
    assert ledger.spent() == pytest.approx(0.15)


# --- cost guard -----------------------------------------------------------------

def guard(cap, *, trial=5.0, trial_spent=0.0, context=800, max_out=500):
    stops = []
    g = CostGuard(Budget([Pot("session", cap), Pot("trial", trial, spent_usd=trial_spent)]), RATES,
                  max_output_tokens=max_out, base_context_tokens=context, on_stop=stops.append)
    return g, stops


def test_no_paid_session_opens_when_the_first_turn_cannot_be_afforded():
    g, stops = guard(cap=0.005)                       # first-turn ceiling is about $0.0084
    assert not g.open()
    assert stops == ["cap"] and g.stopped == "cap"
    assert not g.admit_audio(0.02)


def test_cap_exhausted_mid_session_stops_paid_work():
    g, stops = guard(cap=0.012)
    assert g.open()
    assert all(g.admit_audio(0.02) for _ in range(100))  # two seconds of speech
    # The first turn's actual cost leaves too little for the next turn's ceiling.
    # actual $0.0033 + next ceiling (1,100 tokens of context) $0.0093 > $0.012
    g.on_usage(usage(prompt_text=800, prompt_audio=100, out_audio=200))
    assert g.stopped == "cap" and stops == ["cap"]
    assert not g.admit_audio(0.02) and not g.admit_audio(0.02)
    assert g.audio_seconds_refused == pytest.approx(0.04)
    assert stops == ["cap"]                           # fires once


def test_audio_admission_refused_when_headroom_runs_out_mid_utterance():
    g, stops = guard(cap=0.0090)                     # ceiling ~$0.0084, ~8 s of audio left
    assert g.open()
    admitted = 0
    for _ in range(10_000):                           # bounded: a broken guard fails, not hangs
        if not g.admit_audio(0.02):
            break
        admitted += 1
    assert 0 < admitted < 500 and stops == ["cap"]
    assert g.budget.pots[0].spent_usd + g.budget.pots[0].reserved_usd <= 0.0090 + 1e-9


def test_trial_total_stops_a_session_that_still_has_room():
    g, stops = guard(cap=0.50, trial=5.0, trial_spent=4.995)
    assert not g.open() and stops == ["cap"]


def test_usage_after_stop_is_still_charged():
    g, _ = guard(cap=0.012)
    g.open()
    g.on_usage(usage(prompt_text=800, out_audio=300))
    assert g.stopped
    before = g.budget.pots[0].spent_usd
    g.on_usage(usage(out_audio=100))                  # an in-flight turn reports late
    assert g.budget.pots[0].spent_usd == pytest.approx(before + 100 * 12 / 1e6)


def test_settling_releases_reservations_and_reserves_the_next_turn():
    g, _ = guard(cap=0.50)
    g.open()
    g.admit_audio(1.0)
    g.on_usage(usage(prompt_text=800, prompt_audio=25, out_audio=50))
    assert g.audio == [] and g.turn is not None
    assert g.context_tokens == 875
    assert g.budget.pots[0].reserved_usd == pytest.approx(g.turn_ceiling())
    g.close()
    assert g.budget.pots[0].reserved_usd == 0 and g.turn is None


def test_compact_instructions_stay_under_two_thousand_tokens():
    text = instructions()
    assert estimate_tokens(text) < 2000
    assert "laugh" in text and "Never say stage directions aloud" in text


# --- idle -----------------------------------------------------------------------

def test_idle_watchdog_expires_only_after_silence():
    clock = Clock()
    idle = IdleWatchdog(10, clock)
    clock.t += 9
    assert not idle.expired()
    idle.touch()
    clock.t += 9.9
    assert not idle.expired()
    clock.t += 0.2
    assert idle.expired()


# --- transcript -----------------------------------------------------------------

def test_transcript_keeps_generated_played_and_interrupted_apart():
    events = []
    t = Transcript(sink=events.append, clock=Clock())
    t.user("Tell me a story.")
    t.generated("g1", "Once upon a time, ")
    t.generated("g1", "a sage walked into a very long forest.")
    t.played("g1", "Once upon a time,", interrupted=True)
    t.user("Stop. Say mango.")
    t.generated("g2", "Mango.")
    t.played("g2", "Mango.", interrupted=False)
    t.generated("g3", "Anything else?")          # generated, never played
    t.finish()

    states = {e["gen_id"]: e["state"] for e in t.entries() if e["role"] == "assistant"}
    assert states == {"g1": INTERRUPTED, "g2": PLAYED, "g3": UNPLAYED}
    g1 = next(e for e in t.entries() if e["gen_id"] == "g1")
    assert g1["generated"].endswith("forest.") and g1["text"] == "Once upon a time,"
    assert t.heard() == [
        {"role": "user", "text": "Tell me a story.", "interrupted": False},
        {"role": "assistant", "text": "Once upon a time,", "interrupted": True},
        {"role": "user", "text": "Stop. Say mango.", "interrupted": False},
        {"role": "assistant", "text": "Mango.", "interrupted": False},
    ]
    assert [e["kind"] for e in events].count("transcript_assistant") == 3


def test_played_without_seen_generation_is_still_recorded():
    t = Transcript(clock=Clock())
    t.played("unknown", "Hello.", interrupted=False)
    assert t.heard() == [{"role": "assistant", "text": "Hello.", "interrupted": False}]


# --- latency --------------------------------------------------------------------

def test_latency_from_last_voiced_input_to_first_model_audio():
    clock = Clock(10.0)
    m = LatencyMeter(threshold=300, clock=clock)
    m.on_input(2000)
    clock.t = 10.5
    m.on_input(1000)                                  # last voiced chunk
    clock.t = 10.6
    m.on_input(10)                                    # silence does not move end of speech
    clock.t = 11.4
    assert m.on_model_audio("g1") == {"gen_id": "g1", "eos_to_first_model_audio_ms": 900}
    assert m.on_model_audio("g1") is None             # later chunks of the same reply
    clock.t = 12.0
    assert m.on_model_audio("g2") is None             # no new speech: not counted


def test_rms_reads_int16_memoryviews_by_bytes():
    samples = array("h", [3000, -3000] * 240)
    assert pcm16_rms(memoryview(samples)) == pytest.approx(3000)
    assert pcm16_rms(bytes(10)) == 0


# --- the money holes (Codex review, 2026-11-04) ------------------------------------

def test_every_reply_holds_a_ceiling_even_with_no_usage_reports():
    """Before: the next turn was reserved only on a usage report, so with no
    reports only the first reply was ever bounded. Now each reply reserves
    when it starts, and the cap stops the call."""
    g, stops = guard(cap=0.05)
    assert g.open()
    started = 0
    for i in range(100):                              # bounded: a broken guard fails, not hangs
        if not g.on_generation(f"g{i}"):
            break
        started += 1
    assert 0 < started < 10 and stops == ["cap"]
    session = g.budget.pots[0]
    assert session.reserved_usd <= session.cap_usd + 1e-9
    charged = g.close()                               # all of it is owed: none was reported
    assert charged == pytest.approx((started + 1) * g.turn_ceiling())
    assert session.spent_usd == pytest.approx(charged) and session.reserved_usd == pytest.approx(0)


def test_a_reply_cut_off_by_the_close_is_charged_at_its_ceiling():
    g, _ = guard(cap=0.50)
    g.open()
    g.on_generation("g1")
    g.admit_audio(1.0)
    charged = g.close()
    assert charged == pytest.approx(g.turn_ceiling() + g.audio_cost(1.0))
    assert g.budget.pots[0].spent_usd == pytest.approx(charged)


def test_a_turn_no_reply_used_is_released_not_charged():
    g, _ = guard(cap=0.50)
    g.open()
    assert g.close() == 0
    assert g.budget.pots[0].spent_usd == 0 and g.budget.pots[0].reserved_usd == 0


def test_a_report_settles_only_its_own_reply():
    g, _ = guard(cap=0.50)
    g.open()
    g.on_generation("g1")
    g.on_generation("g2")                             # g2 began before g1's report
    g.on_usage(usage(prompt_text=800, out_audio=100), "g1")
    assert list(g.started) == ["g2"]
    assert g.close() == pytest.approx(g.turn_ceiling(), rel=0.2)   # g2 is still owed


def test_a_reported_reply_is_not_charged_again_at_close():
    g, _ = guard(cap=0.50)
    g.open()
    g.on_generation("g1")
    g.on_usage(usage(prompt_text=800, out_audio=100), "g1")
    spent = g.budget.pots[0].spent_usd
    assert g.close() == 0 and g.budget.pots[0].spent_usd == spent


def test_a_corrupt_ledger_refuses_instead_of_reading_zero(tmp_path):
    path = tmp_path / "ledger.json"
    ledger = Ledger(path)
    ledger.add("a", 4.90)
    path.write_text('{"spent_usd": 4.9, "sess', encoding="utf-8")   # a write cut off
    with pytest.raises(LedgerError):
        ledger.spent()
    with pytest.raises(LedgerError):
        ledger.add("b", 0.01)
    assert path.read_text(encoding="utf-8") == '{"spent_usd": 4.9, "sess'   # history kept


def test_ledger_writes_are_atomic_and_serialised_across_writers(tmp_path):
    import threading
    path = tmp_path / "ledger.json"
    ledgers = [Ledger(path) for _ in range(8)]        # as if from separate processes
    errors = []

    def write(lg):
        try:
            for _ in range(10):
                lg.add("c", 0.01)
        except Exception as exc:  # a dead writer must fail the test, not vanish
            errors.append(repr(exc))

    threads = [threading.Thread(target=write, args=(lg,)) for lg in ledgers]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    assert Ledger(path).spent() == pytest.approx(0.80)
    assert len(json.loads(path.read_text(encoding="utf-8"))["sessions"]) == 80
    assert not list(tmp_path.glob("*.tmp")) and not list(tmp_path.glob("*.lock"))


def test_a_stale_lock_is_broken_and_a_live_one_times_out(tmp_path):
    import os
    import time
    path = tmp_path / "ledger.json"
    lock = tmp_path / "ledger.json.lock"
    lock.write_text("", encoding="utf-8")
    old = time.time() - 120
    os.utime(lock, (old, old))                        # its holder died two minutes ago
    assert Ledger(path).add("a", 0.01) == pytest.approx(0.01)
    lock.write_text("", encoding="utf-8")             # a live holder
    with pytest.raises(LedgerError):
        Ledger(path, lock_timeout=0.2).add("b", 0.01)


def test_close_keeps_unreported_replies_and_trailing_audio_apart():
    g, _ = guard(cap=0.50)
    g.open()
    g.on_generation("g1")
    g.on_usage(usage(prompt_text=800, out_audio=100), "g1")   # every reply reported
    g.admit_audio(2.0)                                         # then silence until the idle close
    g.close()
    assert g.charged_replies_at_close == 0
    assert g.charged_audio_at_close == pytest.approx(g.audio_cost(2.0))


def test_a_lock_that_will_not_delete_does_not_undo_a_committed_write(tmp_path, monkeypatch):
    """Before: the error after a good write made the caller retry it, and
    $0.02 became $0.04."""
    import pathlib
    real_unlink = pathlib.Path.unlink

    def stuck(self, *a, **k):
        if self.name.endswith(".lock"):
            raise PermissionError(13, "in use")
        return real_unlink(self, *a, **k)
    monkeypatch.setattr(pathlib.Path, "unlink", stuck)
    monkeypatch.setattr(Ledger, "TRANSIENT_TRIES", 2)
    ledger = Ledger(tmp_path / "ledger.json")
    assert ledger.add("a", 0.02) == pytest.approx(0.02)   # no error raised
    monkeypatch.setattr(pathlib.Path, "unlink", real_unlink)
    assert Ledger(tmp_path / "ledger.json").spent() == pytest.approx(0.02)


def test_the_month_starts_at_local_midnight_on_the_first():
    from datetime import datetime

    from prana.voice.gemini.core import month_start

    mid = datetime(2026, 11, 17, 15, 30).timestamp()
    assert datetime.fromtimestamp(month_start(mid)) == datetime(2026, 11, 1, 0, 0)
    first = datetime(2026, 12, 1, 0, 0, 5).timestamp()
    assert datetime.fromtimestamp(month_start(first)) == datetime(2026, 12, 1, 0, 0)


def test_the_ledger_totals_since_a_time(tmp_path):
    ledger = Ledger(tmp_path / "ledger.json")
    ledger.add("a", 1.0)
    import time
    cut = time.time() + 0.001
    time.sleep(0.01)
    assert ledger.add("b", 2.0, since=cut) == pytest.approx(2.0)
    assert ledger.spent() == pytest.approx(3.0)
    assert ledger.spent(since=cut) == pytest.approx(2.0)
