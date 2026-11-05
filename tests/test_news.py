"""The News desk (redesign L2d): sections, ranking, staleness, failures."""
import json
from datetime import datetime, timezone

import pytest

from prana import news

NOW = datetime(2026, 11, 5, 7, 0, tzinfo=timezone.utc)

REPLY = json.dumps({"sections": [
    {"name": "World", "items": [
        {"title": "Treaty signed", "source": "Reuters", "url": "https://r.example/1", "why": "big", "relevance": 0.4}]},
    {"name": "Video games", "items": [
        {"title": "Minor patch", "source": "IGN", "url": "https://i.example/2", "why": "meh", "relevance": 0.3},
        {"title": "New Zelda", "source": "IGN", "url": "https://i.example/1", "why": "he loves it", "relevance": 0.95}]},
    {"name": "Sport", "items": [{"title": "Not a section", "url": "https://x.example", "relevance": 1}]},
    {"name": "Local", "items": [{"title": "No link", "url": "", "relevance": 0.9}]},
]})


def test_an_edition_is_ranked_by_relevance_and_cleaned(tmp_path):
    out = news.run(NOW, compose_fn=lambda *a, **k: "Here you go:\n" + REPLY, path=tmp_path / "news.json")
    secs = out["editions"][0]["sections"]
    assert [s["name"] for s in secs] == ["Video games", "World"]   # the best story leads; Sport and link-less Local dropped
    assert [i["title"] for i in secs[0]["items"]] == ["New Zelda", "Minor patch"]
    assert out["last_error"] is None


def test_a_failed_run_keeps_the_last_good_edition_and_says_so(tmp_path):
    path = tmp_path / "news.json"
    news.run(NOW, compose_fn=lambda *a, **k: REPLY, path=path)

    def broken(*a, **k):
        raise RuntimeError("search down")
    out = news.run(NOW, compose_fn=broken, path=path)
    assert out["editions"][0]["sections"]                      # the good edition stays
    assert "search down" in out["last_error"]["error"]
    served = news.latest(path, now=NOW.timestamp() + 3600)
    assert served["edition"] and served["stale"] is False and served["last_error"]
    assert news.latest(path, now=NOW.timestamp() + 9 * 3600)["stale"] is True


def test_editions_are_capped_and_garbage_is_refused(tmp_path):
    path = tmp_path / "news.json"
    for _ in range(5):
        news.run(NOW, compose_fn=lambda *a, **k: REPLY, path=path)
    assert len(json.loads(path.read_text(encoding="utf-8"))["editions"]) == news.KEEP_EDITIONS
    with pytest.raises(ValueError):
        news.parse_edition("no json here")


def test_the_prompt_reads_his_interests_from_the_private_tree(tmp_path, monkeypatch):
    monkeypatch.setattr(news, "interests_path", lambda: tmp_path / "interests.md")
    (tmp_path / "interests.md").write_text("- Video games above all", encoding="utf-8")
    p = news.system_prompt(NOW)
    assert "Video games above all" in p and "World; Local" in p
