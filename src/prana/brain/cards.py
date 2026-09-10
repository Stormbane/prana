"""Auto-cards (plan §T4): when the agent's tool use lands a fitness
write or read, the brain mints a card for the transcript.

The claude-agent-sdk streams every ``ToolUseBlock`` (name + input)
and its ``ToolResultBlock`` to the backend, so no hooks are needed:
the backend pairs them and asks this module for a card. Cards use the
shell card envelope the app already renders (card_id, kind, v, as_of,
caption, data) — kind "kv" for now, which the fallback renderer draws
as caption + key/values; richer renderers can claim the same kinds
later without a wire change.
"""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

CARD_V = 1

# MCP tool names arrive prefixed (mcp__akhada__log_meal); match on the
# bare name so a server rename doesn't silence cards.
_INTERESTING = {
    "log_meal", "add_meal_items", "log_workout", "log_set", "log_vital", "log_weight",
    "get_today_summary", "get_recent", "get_exercise_history",
    "notes_create", "notes_update", "notes_check_item",
}


@dataclass
class CardEvent:
    """A card to place in the transcript, yielded from a backend's
    run_turn between text chunks."""

    spec: dict[str, Any] = field(default_factory=dict)


@dataclass
class ActivityEvent:
    """A tool-activity chip, yielded the moment a tool call starts
    (Suti 2026-09-08: "when a web search happens a little chip comes
    up… music playing… bundle into a section"). ``activity`` is
    {"text": short label, "kind": search|read|memory|fitness|notes|
    music|files|tool, "id": tool-use id, "state": "running"} — the app
    picks glyph and color by kind. When the call ends a second event
    with the same id carries "state": "done" | "failed" and, if a card
    was minted from the result, its "card_id" (Suti 2026-09-09: chips
    stay in the transcript; tapping one opens the card)."""

    activity: dict[str, str] = field(default_factory=dict)


class ToolTracker:
    """Pairs a turn's tool uses with their results and turns them into
    events — pure, so the pairing is testable without the SDK.

    ``on_use`` yields the chip the moment a call starts; ``on_result``
    yields the card (if the result mints one) and the chip's closing
    state. Calls that got no chip get no closing event either. A
    malformed input never aborts the turn just to label a chip."""

    def __init__(self) -> None:
        self._pending: dict[str, tuple[str, dict]] = {}
        self._chipped: set[str] = set()

    def on_use(self, tool_id: str, name: str, raw_input: Any) -> list[ActivityEvent]:
        tool_input = dict(raw_input) if isinstance(raw_input, dict) else {}
        self._pending[tool_id] = (name, tool_input)
        activity = activity_for(name, tool_input)
        if activity is None:
            return []
        self._chipped.add(tool_id)
        return [ActivityEvent({**activity, "id": tool_id, "state": "running"})]

    def on_result(self, tool_id: str, content: Any,
                  is_error: bool = False) -> list[CardEvent | ActivityEvent]:
        use = self._pending.pop(tool_id, None)
        if use is None:
            return []
        if isinstance(content, list):
            content = "".join(
                p.get("text", "") for p in content
                if isinstance(p, dict) and p.get("type") == "text")
        text = content if isinstance(content, str) else ""
        # akhada and the notes tools report a refused write as ordinary
        # text — {"ok": false, ...} / {"error": ...} — not as a
        # transport error (review): that chip closes failed too.
        failed = is_error or _refused(text)
        events: list[CardEvent | ActivityEvent] = []
        card = None if failed else card_for(use[0], use[1], text)
        if card is not None:
            card["activity_id"] = tool_id
            events.append(CardEvent(card))
        if tool_id in self._chipped:
            self._chipped.discard(tool_id)
            closing = {"id": tool_id, "state": "failed" if failed else "done"}
            if card is not None:
                closing["card_id"] = card["card_id"]
            events.append(ActivityEvent(closing))
        return events


def _refused(result_text: str) -> bool:
    """The known producers' failure envelopes, in the result's text."""
    try:
        res = json.loads(result_text)
    except (TypeError, ValueError):
        return False
    return isinstance(res, dict) and (res.get("ok") is False or bool(res.get("error")))


def bare_tool_name(name: str) -> str:
    return name.rsplit("__", 1)[-1] if "__" in name else name


def _server_of(name: str) -> str:
    """mcp__akhada__log_meal → akhada; builtins → ""."""
    parts = name.split("__")
    return parts[1] if len(parts) >= 3 and parts[0] == "mcp" else ""


def _short(value: Any, n: int = 48) -> str:
    s = str(value or "").strip().replace("\n", " ")
    return s if len(s) <= n else s[: n - 1] + "…"


_FITNESS_VERBS = {
    "log_meal": "Logging a meal", "add_meal_items": "Adding to a meal",
    "log_workout": "Logging a workout", "log_set": "Logging a set",
    "log_vital": "Logging a vital", "log_weight": "Logging weight",
    "log_habit": "Logging a habit", "get_today_summary": "Checking today",
    "get_recent": "Checking recent days", "search_food_log": "Searching the food log",
    "remember_food": "Remembering a food", "void_entry": "Voiding an entry",
    "amend_entry": "Amending an entry", "get_goals": "Checking goals",
}


def _host_of(url: Any) -> str:
    """Hostname only — never userinfo, port, path or query, any of
    which can carry a credential (review)."""
    from urllib.parse import urlsplit

    try:
        host = urlsplit(str(url or "")).hostname or ""
    except ValueError:
        host = ""
    return _short(host, 40) or "a page"


_FILE_VERBS = {
    "Read": "Reading a file", "Glob": "Finding files", "Grep": "Searching files",
    "Edit": "Editing a file", "Write": "Writing a file",
    "Bash": "Running a command", "PowerShell": "Running a command",
}


def activity_for(tool_name: str, tool_input: Any) -> dict[str, str] | None:
    """Label + kind for a tool call, or None for calls not worth a chip
    (TodoWrite, ToolSearch and the like).

    Disclosure policy (review): the phone sees WHAT KIND of thing is
    happening, plus the user's own search/memory query. Never a URL
    beyond its hostname, never a command line, never a file path —
    those are the agent's workings and can carry secrets."""
    inp = tool_input if isinstance(tool_input, dict) else {}
    bare = bare_tool_name(tool_name)
    server = _server_of(tool_name)
    if tool_name == "WebSearch":
        return {"text": f"Web search: {_short(inp.get('query'))}", "kind": "search"}
    if tool_name == "WebFetch":
        return {"text": f"Reading {_host_of(inp.get('url'))}", "kind": "read"}
    if tool_name in _FILE_VERBS:
        return {"text": _FILE_VERBS[tool_name], "kind": "files"}
    if server == "smriti" or bare.startswith("smriti_"):
        if bare in ("smriti_read",):
            return {"text": f"Remembering: {_short(inp.get('query'), 40)}", "kind": "memory"}
        if bare in ("smriti_write", "smriti_record_decision", "smriti_record_finding"):
            return {"text": "Writing memory", "kind": "memory"}
        return {"text": f"Memory: {bare.removeprefix('smriti_').replace('_', ' ')}", "kind": "memory"}
    if server == "akhada" or bare in _FITNESS_VERBS:
        return {"text": _FITNESS_VERBS.get(bare, f"Akhada: {bare.replace('_', ' ')}"),
                "kind": "fitness"}
    if server == "narada-box" or bare.startswith("box_"):
        if bare == "box_show_image":
            return {"text": "Showing it on the box", "kind": "tool"}
        return {"text": f"Box: {bare.removeprefix('box_').replace('_', ' ')}", "kind": "tool"}
    if server in ("narada-notes", "notes") or bare.startswith("note"):
        return {"text": f"Notes: {bare.replace('_', ' ')}", "kind": "notes"}
    if bare in ("play", "cast_play", "play_music", "play_station"):
        station = _short(inp.get("station") or inp.get("station_query") or inp.get("query"), 30)
        speaker = _short(inp.get("speaker") or inp.get("speaker_query"), 20)
        where = f" on {speaker}" if speaker else ""
        return {"text": f"Playing {station}{where}", "kind": "music"}
    if bare in ("stop", "cast_stop", "stop_music"):
        return {"text": "Stopping the music", "kind": "music"}
    if bare in ("volume", "cast_volume"):
        return {"text": f"Volume {_short(inp.get('percent'), 6)}%", "kind": "music"}
    if bare in ("speak",):
        return {"text": "Speaking through the house", "kind": "music"}
    if tool_name in ("TodoWrite", "ToolSearch", "Task", "Agent", "AskUserQuestion"):
        return None
    if bare.startswith("list_mcp_"):  # Codex's own MCP housekeeping
        return None
    return {"text": bare.replace("_", " "), "kind": "tool"}


def interesting(name: str) -> bool:
    return bare_tool_name(name) in _INTERESTING


def _rng(low: Any, high: Any, mid: Any, unit: str = "") -> str:
    try:
        if low is not None and high is not None and round(float(low)) != round(float(high)):
            return f"{round(float(low))}–{round(float(high))}{unit}"
        v = mid if mid is not None else low
        return "—" if v is None else f"{round(float(v))}{unit}"
    except (TypeError, ValueError):
        return "—"


def _totals_kv(t: dict | None) -> dict[str, str]:
    if not isinstance(t, dict):
        return {}
    return {
        "calories": _rng(t.get("calories_low"), t.get("calories_high"), t.get("calories"), " kcal"),
        "protein": _rng(t.get("protein_low"), t.get("protein_high"), t.get("protein_g"), " g"),
    }


def _result_json(result_text: str) -> Any:
    try:
        return json.loads(result_text)
    except (TypeError, ValueError):
        return None


def _day_link(day: Any) -> dict[str, str]:
    """The Fitness screen, on a day (Suti 2026-09-09: tapping a card
    takes you to the data behind it)."""
    d = str(day or "").strip()
    to = f"/data/fitness?day={d}" if len(d) == 10 and d[4] == "-" else "/data/fitness"
    return {"to": to, "label": "Open the day"}


def _exercise_link(exercise: Any) -> dict[str, str]:
    """ONE route segment (review): quote() keeps '/' by default, so
    'clean/jerk' would have become two segments and missed the app's
    /data/fitness/exercise/:name route."""
    from urllib.parse import quote

    return {"to": f"/data/fitness/exercise/{quote(str(exercise or '').strip(), safe='')}",
            "label": "Progress over time"}


def _note_link(note_id: Any) -> dict[str, str]:
    from urllib.parse import quote

    return {"to": f"/data/notes?note={quote(str(note_id or ''), safe='')}",
            "label": "Open the note"}


def _fmt_kg(value: Any) -> str:
    try:
        return f"{float(value):g} kg"
    except (TypeError, ValueError):
        return ""


def card_for(tool_name: str, tool_input: dict | None, result_text: str) -> dict | None:
    """Return a card spec for an interesting tool call, else None.
    Never raises — a card is candy, the turn is the meal.

    A card may carry ``link`` {to, label}: an in-app route the card
    opens when tapped — the set's exercise history, the meal's day,
    the note itself."""
    name = bare_tool_name(tool_name)
    if name not in _INTERESTING:
        return None
    res = _result_json(result_text)
    if not isinstance(res, dict) or res.get("ok") is False or res.get("error"):
        return None
    tool_input = tool_input or {}
    now = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    base = {"card_id": f"auto-{uuid.uuid4().hex[:10]}", "kind": "kv",
            "v": CARD_V, "as_of": now}
    try:
        if name == "log_set":
            exercise = str(tool_input.get("exercise") or "set").strip()
            weight = _fmt_kg(res.get("weight_kg", tool_input.get("weight_kg")))
            reps = tool_input.get("reps")
            bits = [b for b in (weight, f"× {reps}" if reps is not None else "") if b]
            caption = f"{exercise} — {' '.join(bits)}" if bits else exercise
            data = {}
            if tool_input.get("notes"):
                data["notes"] = _short(tool_input.get("notes"), 60)
            if res.get("bar_kg") is not None:
                data["bar"] = _fmt_kg(res.get("bar_kg"))
            return {**base, "caption": caption, "data": data,
                    "link": _exercise_link(exercise)}
        if name == "get_exercise_history":
            ex = res.get("exercise") or {}
            exercise = str((ex.get("name") if isinstance(ex, dict) else None)
                           or tool_input.get("exercise") or "exercise")
            best = res.get("best") or {}
            data = {"sets": str(len(res.get("sets") or [])),
                    "sessions": str(len(res.get("session_days") or []))}
            if isinstance(best, dict) and best.get("weight_kg") is not None:
                data["best"] = f"{_fmt_kg(best.get('weight_kg'))} × {best.get('reps', '—')} on {best.get('day', '')}".strip()
            return {**base, "caption": f"{exercise} — history", "data": data,
                    "link": _exercise_link(exercise)}
        if name in ("notes_create", "notes_update", "notes_check_item"):
            title = str(res.get("title") or tool_input.get("title") or "note")
            items = [i for i in (res.get("items") or []) if isinstance(i, dict)]
            verb = {"notes_create": "New note", "notes_update": "Updated",
                    "notes_check_item": "Checked off"}[name]
            data = {}
            if items:
                open_items = [i for i in items if not i.get("done")]
                data["items"] = f"{len(open_items)} open of {len(items)}"
                added = tool_input.get("add_items") or tool_input.get("items") or []
                if isinstance(added, list) and added:
                    data["added"] = _short(", ".join(str(a) for a in added), 60)
            elif res.get("text"):
                data["text"] = _short(res.get("text"), 80)
            return {**base, "caption": f"{verb}: {title}", "data": data,
                    "link": _note_link(res.get("id"))}
        if name in ("log_meal", "add_meal_items"):
            items = res.get("items") or tool_input.get("items") or []
            data: dict[str, Any] = {}
            for it in items[:8]:
                if isinstance(it, dict) and it.get("name"):
                    data[str(it["name"])] = _rng(it.get("calories_low"), it.get("calories_high"),
                                                 it.get("calories"), " kcal")
            data.update({f"meal {k}": v for k, v in
                         _totals_kv(res.get("meal_totals") or res.get("totals")).items()})
            data.update({f"day {k}": v for k, v in _totals_kv(res.get("day_totals")).items()})
            label = res.get("label") or tool_input.get("label") or "meal"
            return {**base, "caption": f"Logged {label}", "data": data,
                    "link": _day_link(res.get("day"))}
        if name == "get_today_summary":
            data = dict(_totals_kv(res.get("totals")))
            meals = [m for m in (res.get("meals") or []) if not m.get("voided_at")]
            data["meals"] = str(len(meals))
            rem = res.get("remaining") or {}
            if isinstance(rem, dict) and rem.get("protein_g") is not None:
                data["protein to go"] = _rng(None, None, rem.get("protein_g"), " g")
            return {**base, "caption": f"{res.get('day', 'Today')} so far", "data": data,
                    "link": _day_link(res.get("day"))}
        if name == "get_recent":
            # akhada nests averages: {all_days, logged_days_only}. The
            # logged-days figure is the honest one on a fast/refeed
            # rhythm (an unlogged day is not a zero-calorie day).
            av = res.get("averages") or {}
            grp = av.get("logged_days_only") or av.get("all_days") or av
            data = {
                "avg calories (logged days)": _rng(None, None, grp.get("calories"), " kcal"),
                "avg protein (logged days)": _rng(None, None, grp.get("protein_g"), " g"),
                "days logged": str(sum(1 for d in (res.get("days") or []) if d.get("logged"))),
            }
            return {**base, "caption": f"{res.get('start', '')} → {res.get('end', '')}".strip(" →"),
                    "data": data, "link": {"to": "/data/fitness", "label": "Open Fitness"}}
        if name == "log_workout":
            w = res if res.get("title") else tool_input
            data = {k: str(v) for k, v in (("kind", w.get("kind")), ("duration", w.get("duration_min")))
                    if v is not None}
            return {**base, "caption": f"Logged {w.get('title') or 'workout'}", "data": data,
                    "link": _day_link(res.get("day") or w.get("day"))}
        if name in ("log_vital", "log_weight"):
            v = res if res.get("kind") or res.get("value") is not None else tool_input
            kind = str(v.get("kind") or ("weight" if name == "log_weight" else "vital"))
            return {**base, "caption": f"Logged {kind.replace('_', ' ')}",
                    "data": {kind: f"{v.get('value', '—')} {v.get('unit', '')}".strip()}}
    except Exception:  # noqa: BLE001 — candy never breaks the turn
        return None
    return None
