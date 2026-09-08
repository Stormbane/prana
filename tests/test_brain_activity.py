"""Activity chips: one per tool call, labelled by what the call is."""

from __future__ import annotations

from prana.brain.cards import activity_for


def test_web_search_and_fetch_become_search_and_read_chips():
    assert activity_for("WebSearch", {"query": "Brisbane weather"}) == {
        "text": "Web search: Brisbane weather", "kind": "search"}
    assert activity_for("WebFetch", {"url": "https://www.abc.net.au/news/x"}) == {
        "text": "Reading www.abc.net.au", "kind": "read"}


def test_mcp_servers_map_to_their_kinds():
    assert activity_for("mcp__akhada__log_meal", {}) == {
        "text": "Logging a meal", "kind": "fitness"}
    assert activity_for("mcp__akhada__list_desires", {})["kind"] == "fitness"
    assert activity_for("mcp__smriti__smriti_read", {"query": "what did we decide"}) == {
        "text": "Remembering: what did we decide", "kind": "memory"}
    assert activity_for("mcp__smriti__smriti_write", {})["text"] == "Writing memory"
    assert activity_for("mcp__narada-notes__note_add", {})["kind"] == "notes"
    assert activity_for("mcp__house__play", {"station": "Groove Salad", "speaker": "kitchen"}) == {
        "text": "Playing Groove Salad on kitchen", "kind": "music"}


def test_housekeeping_tools_get_no_chip_and_unknowns_get_a_plain_one():
    assert activity_for("TodoWrite", {}) is None
    assert activity_for("ToolSearch", {}) is None
    assert activity_for("mcp__thing__do_something", {}) == {
        "text": "do something", "kind": "tool"}


def test_labels_are_short():
    long = "x" * 200
    text = activity_for("WebSearch", {"query": long})["text"]
    assert len(text) < 70 and text.endswith("…")
