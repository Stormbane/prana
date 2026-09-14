"""Voice faults said plainly (Suti 2026-09-14)."""

from __future__ import annotations

import datetime as dt
import json

from prana.voice import faults


class _APIError(Exception):
    def __init__(self, message, body=None, code=None):
        super().__init__(message)
        self.body = body
        self.code = code


def test_the_credit_refusal_from_the_field_log_is_named_plainly():
    err = _APIError("GPT-Live returned an error",
                    body={"type": "invalid_request_error", "code": "credit_balance_exhausted",
                          "message": "You have no credits remaining. Add credits to continue"})
    f = faults.classify(err)
    assert f.code == "credits"
    assert "credit is empty" in f.text and "platform.openai.com" in f.text


def test_connection_noise_after_a_refusal_does_not_win():
    tr = faults.FaultTracker()
    tr.note(_APIError("OpenAI Live API connection closed unexpectedly"))
    assert tr.current.code == "connect"
    tr.note(_APIError("x", body={"code": "credit_balance_exhausted"}))
    assert tr.current.code == "credits"
    tr.note(_APIError("OpenAI Live API connection failed after 3 attempts"))
    assert tr.current.code == "credits"  # the worst seen stays
    tr.clear()
    assert tr.current is None


def test_other_families_and_the_cause_chain():
    assert faults.classify("Incorrect API key provided").code == "auth"
    assert faults.classify(_APIError("429 Too Many Requests")).code == "rate"
    assert faults.classify(_APIError("model_not_found: gpt-live-9")).code == "model"
    inner = _APIError("no credits remaining")
    outer = _APIError("connection failed after 3 attempts")
    outer.__cause__ = inner
    assert faults.classify(outer).code == "credits"
    assert faults.classify(object()).code == "unknown"


def test_wire_shapes_set_and_clear():
    f = faults.Fault("credits", faults.TEXT["credits"])
    assert faults.box_chip(f) == {"type": "chip", "key": "fault", "icon": "tool", "text": f.text}
    assert faults.box_chip(None)["text"] == ""
    assert faults.phone_message(f) == {"type": "fault", "code": "credits", "text": f.text, "sticky": True}
    assert faults.phone_message(None) == {"type": "fault", "code": "", "text": ""}
    assert faults.event_id(f, dt.date(2026, 9, 14)) == "voice-fault:credits:2026-09-14"


def test_notify_posts_a_deduplicated_error_entry(monkeypatch):
    posted = {}

    class _Res:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def fake_urlopen(req, timeout=0):
        posted["url"] = req.full_url
        posted["auth"] = req.get_header("Authorization")
        posted["body"] = json.loads(req.data)
        return _Res()

    monkeypatch.setattr(faults.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr("prana.brain.tokens.load_brain_tokens", lambda: {"prana": "tok-prana"})
    f = faults.Fault("credits", faults.TEXT["credits"])
    assert faults.notify_brain(f, "box", brain_url="http://brain.test/") is True
    assert posted["url"] == "http://brain.test/v1/narada/notify"
    assert posted["auth"] == "Bearer tok-prana"
    assert posted["body"]["level"] == "error" and posted["body"]["source"] == "voice-box"
    assert posted["body"]["event_id"].startswith("voice-fault:credits:")
    # a missing token or a dead brain never raises
    monkeypatch.setattr("prana.brain.tokens.load_brain_tokens", lambda: {})
    assert faults.notify_brain(f, "box", brain_url="http://brain.test/") is False


def test_the_providers_own_words_in_the_plugin_log_outrank_a_generic_exception():
    # The field case: the plugin logs the real error as a record extra and
    # raises APIError("GPT-Live returned an error") with no body.
    import logging

    faults._forget_recent()
    tr = faults.FaultTracker()  # installs the tap
    logging.getLogger("livekit.plugins.openai").error(
        "gpt-live returned an error",
        extra={"lk.pii.error": {"type": "invalid_request_error", "code": "credit_balance_exhausted",
                                "message": "You have no credits remaining."}})
    tr.note(_APIError("GPT-Live returned an error"))
    assert tr.current.code == "credits"
    tr.note(_APIError("OpenAI Live API connection closed unexpectedly"))
    assert tr.current.code == "credits"
    # and the memory is short: an old provider error does not colour a new session
    faults._forget_recent()
    faults._recent().append((faults._time.monotonic() - 999, 0, json.dumps({"code": "credit_balance_exhausted"})))
    fresh = faults.FaultTracker()
    fresh.note(_APIError("connection failed"))
    assert fresh.current.code == "connect"


def test_provider_errors_are_scoped_to_the_session_that_saw_them():
    # Review: a fresh tracker after a credit error must not report credits
    # for its own model_not_found; and a tracker consumes what it reads.
    import logging
    import threading

    faults._forget_recent()
    plugin = logging.getLogger("livekit.plugins.openai")
    plugin.error("gpt-live returned an error",
                 extra={"lk.pii.error": {"code": "credit_balance_exhausted"}})
    later = faults.FaultTracker()  # created after the error: not its fault
    later.note(_APIError("model_not_found: gpt-live-9"))
    assert later.current.code == "model"

    first = faults.FaultTracker()
    plugin.error("gpt-live returned an error",
                 extra={"lk.pii.error": {"code": "credit_balance_exhausted"}})
    first.note(_APIError("GPT-Live returned an error"))
    assert first.current.code == "credits"
    second = faults.FaultTracker()  # sequential session: the buffer was drained
    second.note(_APIError("GPT-Live returned an error"))
    assert second.current.code == "unknown"

    # a concurrent job in another thread has its own buffer
    seen = {}

    def other_job():
        tr = faults.FaultTracker()
        plugin.error("gpt-live returned an error",
                     extra={"lk.pii.error": {"code": "invalid_api_key"}})
        seen["other"] = tr.note(_APIError("GPT-Live returned an error")).code

    t = threading.Thread(target=other_job)
    t.start()
    t.join()
    assert seen["other"] == "auth"
    mine = faults.FaultTracker()
    mine.note(_APIError("GPT-Live returned an error"))
    assert mine.current.code == "unknown"  # the other thread's error stayed there


def test_the_fault_outlives_the_job_until_a_session_speaks(tmp_path, monkeypatch):
    monkeypatch.setenv("NARADA_STATE_DIR", str(tmp_path))
    assert faults.remembered() is None
    f = faults.Fault("credits", faults.TEXT["credits"])
    faults.remember(f)
    assert faults.remembered() == f
    assert faults.box_chip(faults.remembered())["text"] == f.text
    (tmp_path / "voice-fault.json").write_text("{not json", encoding="utf-8")
    assert faults.remembered() is None  # a torn file is no fault
    faults.remember(f)
    faults.forget()
    assert faults.remembered() is None
    faults.forget()  # idempotent
    assert faults.box_chip(faults.remembered())["text"] == ""
