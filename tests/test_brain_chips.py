"""Chips that stay (Suti 2026-09-09): every tool call is a chip with an
id and a state; its result closes the chip and, when it minted a card,
links the two. Cards carry an in-app link to the data behind them.
And the phone chat's charter: no code edits from a chat."""

from __future__ import annotations

import json

from prana.brain.cards import ActivityEvent, CardEvent, ToolTracker, card_for
from prana.brain.charter import PHONE_CHARTER
from prana.brain.config import BrainConfig


def _events(tr: ToolTracker, tool_id: str, name: str, inp: dict, result: dict,
            is_error: bool = False):
    started = tr.on_use(tool_id, name, inp)
    ended = tr.on_result(tool_id, json.dumps(result), is_error)
    return started, ended


def test_a_chip_opens_running_and_closes_done_with_its_card():
    tr = ToolTracker()
    started, ended = _events(
        tr, "toolu_1", "mcp__akhada__log_set",
        {"exercise": "lat pull down", "reps": 12, "weight_kg": 60, "notes": "neutral grip"},
        {"ok": True, "set_id": 9, "workout_id": 5, "weight_kg": 60.0, "bar_kg": None})
    assert started == [ActivityEvent({"text": "Logging a set", "kind": "fitness",
                                      "id": "toolu_1", "state": "running"})]
    assert [type(e) for e in ended] == [CardEvent, ActivityEvent]
    card = ended[0].spec
    assert card["caption"] == "lat pull down — 60 kg × 12"
    assert card["data"] == {"notes": "neutral grip"}
    assert card["link"] == {"to": "/data/fitness/exercise/lat%20pull%20down",
                            "label": "Progress over time"}
    assert card["activity_id"] == "toolu_1"
    assert ended[1] == ActivityEvent({"id": "toolu_1", "state": "done",
                                      "card_id": card["card_id"]})


def test_a_failed_call_closes_failed_and_mints_no_card():
    tr = ToolTracker()
    _, ended = _events(tr, "toolu_2", "mcp__akhada__log_set", {"exercise": "x"},
                       {"error": "no open workout"}, is_error=True)
    assert ended == [ActivityEvent({"id": "toolu_2", "state": "failed"})]


def test_calls_without_a_chip_close_silently_and_unknown_results_are_ignored():
    tr = ToolTracker()
    assert tr.on_use("toolu_3", "TodoWrite", {}) == []
    assert tr.on_result("toolu_3", "ok") == []
    assert tr.on_result("never-seen", "ok") == []


def test_malformed_inputs_and_list_results_never_raise():
    tr = ToolTracker()
    assert tr.on_use("t", "mcp__akhada__log_meal", "not a dict")[0].activity["state"] == "running"
    ended = tr.on_result("t", [{"type": "text", "text": json.dumps(
        {"ok": True, "day": "2026-09-09", "meal_totals": {"calories": 300}})}])
    assert isinstance(ended[0], CardEvent)
    assert ended[0].spec["link"] == {"to": "/data/fitness?day=2026-09-09", "label": "Open the day"}


def test_note_cards_link_to_the_note():
    res = {"id": "n-abc", "title": "Shopping", "kind": "checklist",
           "items": [{"id": "i1", "text": "milk", "done": False},
                     {"id": "i2", "text": "eggs", "done": True}]}
    spec = card_for("mcp__narada-notes__notes_update", {"note_id": "n-abc", "add_items": ["milk"]},
                    json.dumps(res))
    assert spec["caption"] == "Updated: Shopping"
    assert spec["data"] == {"items": "1 open of 2", "added": "milk"}
    assert spec["link"] == {"to": "/data/notes?note=n-abc", "label": "Open the note"}
    # a refused write mints nothing
    assert card_for("mcp__narada-notes__notes_update", {}, json.dumps({"error": "no such note"})) is None


def test_exercise_history_card():
    res = {"ok": True, "exercise": {"id": 3, "name": "lat pull down"},
           "sets": [{"id": 1}, {"id": 2}], "session_days": ["2026-09-09"],
           "best": {"weight_kg": 60.0, "reps": 12, "day": "2026-09-09"}}
    spec = card_for("mcp__akhada__get_exercise_history", {"exercise": "lpd"}, json.dumps(res))
    assert spec["caption"] == "lat pull down — history"
    assert spec["data"] == {"sets": "2", "sessions": "1", "best": "60 kg × 12 on 2026-09-09"}
    assert spec["link"]["to"] == "/data/fitness/exercise/lat%20pull%20down"


def test_the_charter_rides_the_system_prompt_and_editing_tools_are_off(tmp_path, monkeypatch):
    wake = tmp_path / "wake.md"
    wake.write_text("I am Narada.\n", encoding="utf-8")
    monkeypatch.delenv("NARADA_BRAIN_CODE_EDITS", raising=False)
    cfg = BrainConfig(wake_context=wake)
    appended = cfg.system_append()
    assert appended.startswith("I am Narada.")
    assert PHONE_CHARTER.strip() in appended
    assert "smriti_write" in PHONE_CHARTER and "cannot change code" in PHONE_CHARTER
    assert cfg.disallowed_tools[:4] == ["Edit", "Write", "MultiEdit", "NotebookEdit"]
    assert BrainConfig(wake_context=wake, code_edits=True).disallowed_tools == []


def test_a_refused_write_closes_failed_even_without_the_transport_flag():
    # akhada reports "no open workout" as ordinary text, not an error
    # result (review): the chip must not say done.
    tr = ToolTracker()
    _, ended = _events(tr, "toolu_4", "mcp__akhada__log_set", {"exercise": "x"},
                       {"ok": False, "error": "no open workout today"})
    assert ended == [ActivityEvent({"id": "toolu_4", "state": "failed"})]
    tr = ToolTracker()
    _, ended = _events(tr, "toolu_5", "mcp__narada-notes__notes_update", {"note_id": "n"},
                       {"error": "no such note"})
    assert ended == [ActivityEvent({"id": "toolu_5", "state": "failed"})]
    # plain-text results (most builtins) are not failures
    tr = ToolTracker()
    tr.on_use("toolu_6", "WebSearch", {"query": "x"})
    assert tr.on_result("toolu_6", "some prose") == [
        ActivityEvent({"id": "toolu_6", "state": "done"})]


def test_exercise_names_stay_one_route_segment():
    for name in ("clean/jerk", "../../settings", "a b?c#d"):
        spec = card_for("mcp__akhada__log_set", {"exercise": name, "reps": 5},
                        json.dumps({"ok": True, "weight_kg": 20}))
        to = spec["link"]["to"]
        assert to.startswith("/data/fitness/exercise/")
        assert "/" not in to[len("/data/fitness/exercise/"):]
        assert "?" not in to and "#" not in to
    spec = card_for("mcp__narada-notes__notes_create", {"title": "t"},
                    json.dumps({"id": "a/b&c", "title": "t", "items": []}))
    assert spec["link"]["to"] == "/data/notes?note=a%2Fb%26c"


def test_the_phone_chat_has_no_shell_and_no_delegation():
    cfg = BrainConfig(code_edits=False)
    for tool in ("Bash", "PowerShell", "Edit", "Write", "Task", "Agent"):
        assert tool in cfg.disallowed_tools
