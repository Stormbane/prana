"""Voice-safe memory projection — adversarial (cross-review #1).

The load-bearing property: a query that WOULD match private content must
return nothing, because private branches are never searched. If any of
these fail, the voice surface can leak private memory aloud.
"""

from __future__ import annotations

import pytest

from prana.voice import memory
from prana.voice.memory import (
    NEVER_RECALLABLE,
    VOICE_RECALLABLE_BRANCHES,
    recall,
)


@pytest.fixture
def tree(tmp_path):
    """A fake ~/.narada with both private and shareable branches, each
    containing a memory that matches the query 'secret plan'."""
    for branch in ("people", "journal", "identity", "mind", "notes",
                   "projects", "sources"):
        d = tmp_path / branch
        d.mkdir()
        (d / "m.md").write_text(
            f"This {branch} memory is about the secret plan for the project.",
            encoding="utf-8",
        )
    return tmp_path


def test_allowlist_and_denylist_are_disjoint():
    assert set(VOICE_RECALLABLE_BRANCHES).isdisjoint(NEVER_RECALLABLE)


def test_private_branches_never_returned(tree):
    results = recall("secret plan project", root=tree)
    branches = {m.branch for m in results}
    # only allowlisted branches, never the private ones
    assert branches <= set(VOICE_RECALLABLE_BRANCHES)
    for private in ("people", "journal", "identity", "mind"):
        assert private not in branches


def test_shareable_branches_are_recalled(tree):
    results = recall("secret plan project", root=tree)
    assert results, "expected hits from allowlisted branches"
    assert {m.branch for m in results} <= {"projects", "notes", "sources"}


def test_cannot_be_tricked_into_private_branch(tree):
    """Even if a caller passes a private branch explicitly, it's refused."""
    results = recall("secret plan", root=tree,
                     branches=["people", "journal", "notes"])
    branches = {m.branch for m in results}
    assert "people" not in branches and "journal" not in branches
    assert branches <= {"notes"}


def test_secrets_redacted_in_snippets(tree):
    (tree / "notes" / "leak.md").write_text(
        "the deploy key is sk-proj-ABCDEFGHIJKLMNOP1234567890 do not share",
        encoding="utf-8")
    results = recall("deploy key", root=tree)
    for m in results:
        assert "sk-proj-ABCDEFGHIJKLMNOP" not in m.snippet


def test_empty_query_returns_nothing(tree):
    assert recall("", root=tree) == []
    assert recall("a to", root=tree) == []  # all terms too short


def test_missing_branch_dirs_are_fine(tmp_path):
    assert recall("anything", root=tmp_path) == []


# ── path-containment adversarial (cross-review round-2 #2) ────────────

def test_path_traversal_branch_rejected(tree):
    results = recall("secret plan", root=tree,
                     branches=["notes/../people", "notes\\..\\people"])
    assert all(m.branch not in ("people",) for m in results)
    # neither traversal form reaches people
    assert "people" not in {m.branch for m in results}


def test_absolute_branch_path_rejected(tree, tmp_path):
    results = recall("secret plan", root=tree,
                     branches=[str(tree / "people"), "/people"])
    assert "people" not in {m.branch for m in results}


def test_case_variant_denylist_rejected(tree):
    # a case-variant of a private branch must still be refused
    (tree / "People").mkdir(exist_ok=True) if not (tree / "people").exists() else None
    results = recall("secret plan", root=tree, branches=["People", "JOURNAL"])
    assert not {m.branch for m in results} & {"People", "JOURNAL", "people", "journal"}


@pytest.mark.skipif(
    __import__("sys").platform == "win32",
    reason="symlink creation needs admin on Windows; logic covered by resolve()",
)
def test_symlink_into_private_branch_not_followed(tmp_path):
    (tmp_path / "people").mkdir()
    (tmp_path / "people" / "secret.md").write_text(
        "the secret plan lives here", encoding="utf-8")
    (tmp_path / "notes").mkdir()
    # a dir symlink inside an allowlisted branch pointing at the private one
    (tmp_path / "notes" / "sneaky").symlink_to(tmp_path / "people",
                                               target_is_directory=True)
    results = recall("secret plan", root=tmp_path)
    for m in results:
        assert "secret plan lives here" not in m.snippet
