"""Price captured provider usage at official USD rates (OpenAI 2026-09-11, Gemini 2026-11-03)."""
import json
from pathlib import Path
import sys


def estimate(report):
    events = report["events"]
    if report["mode"] == "gemini":
        from gemini_talker import price_usage, rates_for
        rates = rates_for(report["model"])
        usage = [e["usage"] for e in events if e["kind"] == "provider_usage" and e.get("api") == "gemini-live"]
        assert usage, "No Gemini usage captured"
        return {"voice_usd": sum(price_usage(u, rates) for u in usage), "usage_reports": len(usage),
                "basis": "Sum of per-turn usage_metadata at paid-tier rates checked 2026-11-03; "
                         "no cache discount (3.8 Live has none)"}
    if report["mode"] == "live":
        final = [e for e in events if e["kind"] == "provider_usage"
                 and e.get("event_type") == "session.closed"]
        assert len(final) == 1, "Need exactly one confirmed final session usage"
        seconds = final[0]["usage"]["seconds"]
        return {"voice_usd": seconds * .05 / 60, "billable_seconds": seconds,
                "backend_model_usd": 0, "basis": "Final cumulative usage; deterministic client backend"}
    voice = transcription = 0
    seen = set()
    for e in events:
        if e["kind"] != "provider_usage":
            continue
        key = (e["api"], e.get("response_id", e.get("item_id")))
        if key in seen:
            continue
        seen.add(key)
        u = e["usage"]
        assert u is not None, "Missing provider usage"
        if e["api"] == "transcription":
            assert u["type"] == "tokens"
            transcription += (u["input_tokens"] * 1.25 + u["output_tokens"] * 5) / 1e6
            continue
        assert e["api"] == "realtime"
        inp, out = u["input_token_details"], u["output_token_details"]
        cache = inp["cached_tokens_details"]
        assert not inp.get("image_tokens", 0)
        assert inp["text_tokens"] + inp["audio_tokens"] == u["input_tokens"]
        # Reasoning is already included in text_tokens; do not add it twice.
        assert out["text_tokens"] + out["audio_tokens"] == u["output_tokens"]
        assert cache["text_tokens"] + cache["audio_tokens"] == inp["cached_tokens"]
        voice += ((inp["text_tokens"]-cache["text_tokens"]) * .60
                  + cache["text_tokens"] * .06
                  + (inp["audio_tokens"]-cache["audio_tokens"]) * 10
                  + cache["audio_tokens"] * .30
                  + out["text_tokens"] * 2.40 + out["audio_tokens"] * 20) / 1e6
    assert seen, "No usage captured"
    return {"voice_usd": voice, "transcription_usd": transcription,
            "total_usd": voice + transcription, "basis": "Per-response token usage, including cache and reasoning"}


if __name__ == "__main__":
    for name in sys.argv[1:]:
        report = json.loads(Path(name).read_text(encoding="utf-8"))
        print(json.dumps({"call_id": report["call_id"], **estimate(report)}, indent=2))
