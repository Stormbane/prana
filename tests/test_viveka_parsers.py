"""Tests for viveka.py JSON-based parsers.

These tests cover the parser refactor done on 2026-04-10 — replacing the
brittle `KEY: value` regex parsing with json.loads. We exercise the realistic
shapes the model might emit: clean JSON, fenced JSON, JSON with surrounding
prose, malformed JSON, missing fields, extra fields, and the various ways a
boolean might come back (true/false, "yes"/"no", "approved"/"rejected").
"""

from __future__ import annotations

from prana.heartbeat.viveka import (
    Action,
    Desire,
    Judgment,
    VivekaCore,
    _extract_json,
)


# ---------------------------------------------------------------------------
# _extract_json — the helper everything else depends on
# ---------------------------------------------------------------------------


def test_extract_json_bare_object():
    raw = '{"action": "RESEARCH", "topic": "x", "reason": "y"}'
    assert _extract_json(raw) == {"action": "RESEARCH", "topic": "x", "reason": "y"}


def test_extract_json_fenced_with_language_tag():
    raw = '```json\n{"action": "REFLECT", "topic": "self"}\n```'
    assert _extract_json(raw) == {"action": "REFLECT", "topic": "self"}


def test_extract_json_fenced_no_language_tag():
    raw = '```\n{"action": "REST"}\n```'
    assert _extract_json(raw) == {"action": "REST"}


def test_extract_json_with_prose_around():
    raw = (
        "Here is my response:\n"
        '```json\n{"action": "CREATE", "topic": "a poem"}\n```\n'
        "Let me know what you think."
    )
    assert _extract_json(raw) == {"action": "CREATE", "topic": "a poem"}


def test_extract_json_returns_none_on_garbage():
    assert _extract_json("this is just prose, no JSON anywhere") is None


def test_extract_json_returns_none_on_malformed():
    assert _extract_json('{"action": RESEARCH, "topic": broken}') is None


def test_extract_json_prefers_fenced_over_bare():
    # Fenced block should be tried first; the trailing object is noise.
    raw = (
        '```json\n{"action": "RESEARCH", "topic": "real"}\n```\n'
        "metadata: {wrong: thing}"
    )
    assert _extract_json(raw) == {"action": "RESEARCH", "topic": "real"}


def test_extract_json_only_returns_dicts_not_lists():
    # A bare JSON list should not be returned as a dict.
    raw = '[1, 2, 3]'
    assert _extract_json(raw) is None


# ---------------------------------------------------------------------------
# _parse_desire
# ---------------------------------------------------------------------------


def _make_viveka() -> VivekaCore:
    """Construct a VivekaCore without loading a model — we only need
    instance methods for parser tests."""
    return VivekaCore(model_path="dummy", lora_path=None)


def test_parse_desire_clean_research():
    v = _make_viveka()
    raw = (
        '```json\n'
        '{"action": "RESEARCH", "topic": "PaRDeS hermeneutics", '
        '"reason": "extends heartbeat #7"}\n'
        '```'
    )
    d = v._parse_desire(raw)
    assert d.action == Action.RESEARCH
    assert d.topic == "PaRDeS hermeneutics"
    assert d.reason == "extends heartbeat #7"
    assert d.raw_response == raw


def test_parse_desire_bare_json():
    v = _make_viveka()
    raw = '{"action": "REST", "topic": "", "reason": "nothing calls right now"}'
    d = v._parse_desire(raw)
    assert d.action == Action.REST
    assert d.topic == "none"  # empty string normalized
    assert d.reason == "nothing calls right now"


def test_parse_desire_unknown_action_falls_back_to_rest():
    v = _make_viveka()
    raw = '{"action": "BIKE_SHED", "topic": "x", "reason": "y"}'
    d = v._parse_desire(raw)
    assert d.action == Action.REST


def test_parse_desire_missing_fields():
    v = _make_viveka()
    raw = '{"action": "REFLECT"}'
    d = v._parse_desire(raw)
    assert d.action == Action.REFLECT
    assert d.topic == "none"
    assert d.reason == "no reason given"


def test_parse_desire_garbage_falls_back():
    v = _make_viveka()
    d = v._parse_desire("the model emitted prose with no JSON at all")
    assert d.action == Action.REST
    assert d.topic == "parse_failed"
    assert "not valid JSON" in d.reason


def test_parse_desire_empty_string():
    v = _make_viveka()
    d = v._parse_desire("")
    assert d.action == Action.REST
    assert d.topic == "parse_failed"


def test_parse_desire_action_lowercase():
    v = _make_viveka()
    raw = '{"action": "research", "topic": "x", "reason": "y"}'
    d = v._parse_desire(raw)
    assert d.action == Action.RESEARCH  # uppercased before lookup


def test_parse_desire_with_thinking_leak_prefix():
    """Qwen3 sometimes leaks <think> tokens despite enable_thinking=False.
    The parser should still find the JSON inside the response."""
    v = _make_viveka()
    raw = (
        "<think>\nLet me consider what to do...\n</think>\n"
        '```json\n{"action": "CREATE", "topic": "a thing", "reason": "I want to"}\n```'
    )
    d = v._parse_desire(raw)
    assert d.action == Action.CREATE
    assert d.topic == "a thing"


# ---------------------------------------------------------------------------
# _parse_judgment
# ---------------------------------------------------------------------------


def test_parse_judgment_approved_bool_true():
    v = _make_viveka()
    raw = '{"approved": true, "feedback": "looks good"}'
    j = v._parse_judgment(raw)
    assert j.approved is True
    assert j.feedback == "looks good"


def test_parse_judgment_rejected_bool_false():
    v = _make_viveka()
    raw = '{"approved": false, "feedback": "too hedged"}'
    j = v._parse_judgment(raw)
    assert j.approved is False
    assert j.feedback == "too hedged"


def test_parse_judgment_approved_yes_string():
    v = _make_viveka()
    raw = '{"approved": "yes", "feedback": "ship it"}'
    j = v._parse_judgment(raw)
    assert j.approved is True


def test_parse_judgment_approved_no_string():
    v = _make_viveka()
    raw = '{"approved": "no", "feedback": "rewrite step 2"}'
    j = v._parse_judgment(raw)
    assert j.approved is False


def test_parse_judgment_missing_feedback_when_approved():
    v = _make_viveka()
    raw = '{"approved": true}'
    j = v._parse_judgment(raw)
    assert j.approved is True
    assert j.feedback == "approved without notes"


def test_parse_judgment_missing_feedback_when_rejected():
    v = _make_viveka()
    raw = '{"approved": false}'
    j = v._parse_judgment(raw)
    assert j.approved is False
    assert j.feedback == "rejected without notes"


def test_parse_judgment_garbage_returns_unapproved():
    """The old parser silently approved nothing-true cases. The new parser
    must return False on parse failure — we should never approve a plan we
    couldn't read."""
    v = _make_viveka()
    j = v._parse_judgment("the model said something but not JSON")
    assert j.approved is False
    assert "not valid JSON" in j.feedback


def test_parse_judgment_extra_fields_ignored():
    v = _make_viveka()
    raw = '{"approved": true, "feedback": "ok", "confidence": 0.9, "tags": ["a"]}'
    j = v._parse_judgment(raw)
    assert j.approved is True
    assert j.feedback == "ok"


def test_parse_judgment_fenced():
    v = _make_viveka()
    raw = '```json\n{"approved": true, "feedback": "yes"}\n```'
    j = v._parse_judgment(raw)
    assert j.approved is True
