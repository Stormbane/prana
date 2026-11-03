"""Unit tests for the Gemini talker core: no network, no LiveKit server, no spend."""
from array import array

import pytest

from gemini_talker import (
    INTERRUPTED, PLAYED, UNPLAYED, Budget, CostGuard, IdleWatchdog, LatencyMeter, Ledger, Pot,
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
