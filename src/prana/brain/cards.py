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
    "log_meal", "add_meal_items", "log_workout", "log_vital", "log_weight",
    "get_today_summary", "get_recent",
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
    music|files|tool} — the app picks glyph and color by kind."""

    activity: dict[str, str] = field(default_factory=dict)


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


def activity_for(tool_name: str, tool_input: dict | None) -> dict[str, str] | None:
    """Label + kind for a tool call, or None for calls not worth a chip
    (TodoWrite, ToolSearch and the like)."""
    inp = tool_input or {}
    bare = bare_tool_name(tool_name)
    server = _server_of(tool_name)
    if tool_name == "WebSearch":
        return {"text": f"Web search: {_short(inp.get('query'))}", "kind": "search"}
    if tool_name == "WebFetch":
        url = str(inp.get("url") or "")
        host = url.split("//", 1)[-1].split("/", 1)[0] or _short(url)
        return {"text": f"Reading {host}", "kind": "read"}
    if tool_name in ("Read", "Glob", "Grep", "Bash", "Edit", "Write", "PowerShell"):
        return {"text": f"{tool_name}: {_short(inp.get('file_path') or inp.get('pattern') or inp.get('command') or inp.get('description'), 40)}",
                "kind": "files"}
    if server == "smriti" or bare.startswith("smriti_"):
        if bare in ("smriti_read",):
            return {"text": f"Remembering: {_short(inp.get('query'), 40)}", "kind": "memory"}
        if bare in ("smriti_write", "smriti_record_decision", "smriti_record_finding"):
            return {"text": "Writing memory", "kind": "memory"}
        return {"text": f"Memory: {bare.removeprefix('smriti_').replace('_', ' ')}", "kind": "memory"}
    if server == "akhada" or bare in _FITNESS_VERBS:
        return {"text": _FITNESS_VERBS.get(bare, f"Akhada: {bare.replace('_', ' ')}"),
                "kind": "fitness"}
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


def card_for(tool_name: str, tool_input: dict | None, result_text: str) -> dict | None:
    """Return a card spec for an interesting tool call, else None.
    Never raises — a card is candy, the turn is the meal."""
    name = bare_tool_name(tool_name)
    if name not in _INTERESTING:
        return None
    res = _result_json(result_text)
    if not isinstance(res, dict) or res.get("ok") is False:
        return None
    tool_input = tool_input or {}
    now = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    base = {"card_id": f"auto-{uuid.uuid4().hex[:10]}", "kind": "kv",
            "v": CARD_V, "as_of": now}
    try:
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
            return {**base, "caption": f"Logged {label}", "data": data}
        if name == "get_today_summary":
            data = dict(_totals_kv(res.get("totals")))
            meals = [m for m in (res.get("meals") or []) if not m.get("voided_at")]
            data["meals"] = str(len(meals))
            rem = res.get("remaining") or {}
            if isinstance(rem, dict) and rem.get("protein_g") is not None:
                data["protein to go"] = _rng(None, None, rem.get("protein_g"), " g")
            return {**base, "caption": f"{res.get('day', 'Today')} so far", "data": data}
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
                    "data": data}
        if name == "log_workout":
            w = res if res.get("title") else tool_input
            data = {k: str(v) for k, v in (("kind", w.get("kind")), ("duration", w.get("duration_min")))
                    if v is not None}
            return {**base, "caption": f"Logged {w.get('title') or 'workout'}", "data": data}
        if name in ("log_vital", "log_weight"):
            v = res if res.get("kind") or res.get("value") is not None else tool_input
            kind = str(v.get("kind") or ("weight" if name == "log_weight" else "vital"))
            return {**base, "caption": f"Logged {kind.replace('_', ' ')}",
                    "data": {kind: f"{v.get('value', '—')} {v.get('unit', '')}".strip()}}
    except Exception:  # noqa: BLE001 — candy never breaks the turn
        return None
    return None
