"""The lab's own scripts around the Gemini talker (the talker itself is
tested in prana: tests/test_voice_gemini_*.py)."""
import pytest

pytest.importorskip("livekit.plugins.google")


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
