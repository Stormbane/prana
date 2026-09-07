"""Projects overview: todo counts, milestone, newest note."""

from __future__ import annotations

from pathlib import Path

import pytest

from prana.brain import projects


@pytest.fixture(autouse=True)
def temp_home(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
    yield tmp_path


def test_lists_projects_from_both_sources(tmp_path):
    root = tmp_path / ".narada"
    (root / "mirrors" / "alpha" / "ai").mkdir(parents=True)
    (root / "mirrors" / "alpha" / "ai" / "todo.md").write_text(
        "# TODO\n\n## Ship it\n\n- [x] done thing\n- [ ] open one\n- [ ] open two\n",
        encoding="utf-8")
    (root / "projects" / "alpha" / "2026").mkdir(parents=True)
    (root / "projects" / "alpha" / "2026" / "09-07.md").write_text(
        "---\ndate: x\n---\n\n# Older\n", encoding="utf-8")
    (root / "projects" / "alpha" / "2026" / "09-08.md").write_text(
        "---\ndate: x\n---\n\n# First note\n\ntext\n\n# Newest note\n", encoding="utf-8")
    (root / "projects" / "beta" / "2025").mkdir(parents=True)
    (root / "projects" / "beta" / "2025" / "01-01.md").write_text("# Beta\n", encoding="utf-8")

    rows = projects.list_projects()
    assert [r["name"] for r in rows] == ["alpha", "beta"]  # newest activity first
    a = rows[0]
    assert a["todo_open"] == 2 and a["todo_done"] == 1 and a["milestone"] == "Ship it"
    assert a["last_note_at"] == "2026-09-08" and a["last_note_title"] == "Newest note"
    b = rows[1]
    assert b["todo_open"] is None and b["last_note_title"] == "Beta"


def test_empty_home_is_empty_list():
    assert projects.list_projects() == []


def test_current_milestone_prefers_active_then_open_work():
    from prana.brain.projects import current_milestone

    tagged = "## Milestone 0 [DONE]\n- [x] a\n## Milestone 3.5 [IN PROGRESS]\n- [ ] b\n## Later\n- [ ] c\n"
    assert current_milestone(tagged) == "Milestone 3.5 [IN PROGRESS]"
    untagged = "## Done things\n- [x] a\n- [x] b\n## Next up\n- [ ] c\n"
    assert current_milestone(untagged) == "Next up"
    stack = "## Nightly cycle — top of stack\n- [x] a\n## Other\n- [ ] z\n"
    assert current_milestone(stack) == "Nightly cycle — top of stack"
    assert current_milestone("## Only\n- [x] a\n") == "Only"
    assert current_milestone("no headings") is None
