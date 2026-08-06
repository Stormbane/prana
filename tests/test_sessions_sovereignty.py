"""The sovereignty boundary, pinned by tests.

If any of these fail, the voice surface can mutate coding sessions
without judgment — that is a security regression, not a style issue.
"""

from __future__ import annotations

import asyncio

import pytest

import prana.sessions.escalate as escalate_mod
from prana.sessions.escalate import ProposalError, ProposalQueue, judge_with_narada
from prana.sessions.manager import ManagerConfig, SessionManager
from prana.sessions.mcp import build_server


# ── proposal queue ───────────────────────────────────────────────────


@pytest.fixture
def queue(tmp_path):
    return ProposalQueue(tmp_path / "s.db")


def test_propose_decide_redeem_roundtrip(queue):
    p = queue.propose("voice", "spawn_session",
                      {"provider": "claude", "cwd": "C:/p", "prompt": "x"})
    assert p.status == "pending"
    p = queue.decide(p.id, approve=True, decided_by="narada-judge", reason="ok")
    assert p.status == "approved" and p.capability
    p = queue.redeem(p.id, p.capability)
    assert p.status == "executed"


def test_capability_is_single_use(queue):
    p = queue.propose("voice", "cancel_session", {"session_id": "s1"})
    p = queue.decide(p.id, approve=True, decided_by="prana")
    queue.redeem(p.id, p.capability)
    with pytest.raises(ProposalError):
        queue.redeem(p.id, p.capability)  # second use fails


def test_wrong_capability_rejected(queue):
    p = queue.propose("voice", "cancel_session", {"session_id": "s1"})
    queue.decide(p.id, approve=True, decided_by="prana")
    with pytest.raises(ProposalError):
        queue.redeem(p.id, "forged-token")


def test_rejected_proposal_has_no_capability(queue):
    p = queue.propose("voice", "relay_instruction",
                      {"session_id": "s1", "text": "rm -rf"})
    p = queue.decide(p.id, approve=False, decided_by="narada-judge",
                     reason="no")
    assert p.capability is None
    with pytest.raises(ProposalError):
        queue.redeem(p.id, "")


def test_double_decide_rejected(queue):
    p = queue.propose("voice", "cancel_session", {"session_id": "s1"})
    queue.decide(p.id, approve=False, decided_by="prana")
    with pytest.raises(ProposalError):
        queue.decide(p.id, approve=True, decided_by="prana")


def test_only_mutations_are_proposable(queue):
    with pytest.raises(ProposalError):
        queue.propose("voice", "resume_foreign_session", {"session_id": "x"})
    with pytest.raises(ProposalError):
        queue.propose("voice", "list_sessions", {})


# ── judgment fails closed ────────────────────────────────────────────


def test_judge_fails_closed_on_error(queue, monkeypatch):
    p = queue.propose("voice", "spawn_session",
                      {"provider": "claude", "cwd": "C:/p", "prompt": "x"})

    def boom(*a, **k):
        raise OSError("claude not reachable")

    monkeypatch.setattr(escalate_mod, "run_hidden", boom)
    approve, reason = judge_with_narada(p)
    assert approve is False


def test_judge_fails_closed_on_garbage_verdict(queue, monkeypatch):
    p = queue.propose("voice", "spawn_session",
                      {"provider": "claude", "cwd": "C:/p", "prompt": "x"})

    class R:
        returncode = 0
        stdout = "not json at all"

    monkeypatch.setattr(escalate_mod, "run_hidden", lambda *a, **k: R())
    approve, _ = judge_with_narada(p)
    assert approve is False


# ── tier registries ──────────────────────────────────────────────────


def _tool_names(server) -> set[str]:
    tools = asyncio.run(server.list_tools())
    return {t.name for t in tools}


MUTATIONS = {"spawn_session", "relay_instruction", "cancel_session",
             "decide_proposal", "resume_foreign_session"}
READS = {"list_sessions", "session_status", "read_output",
         "list_foreign_sessions", "focus_pane", "proposal_status"}


@pytest.fixture
def mgr(tmp_path):
    return SessionManager(ManagerConfig(db_path=tmp_path / "s.db"))


def test_voice_tier_has_no_mutation_tools(mgr):
    names = _tool_names(build_server("voice", mgr))
    assert MUTATIONS.isdisjoint(names), (
        f"voice tier exposes mutations: {MUTATIONS & names}"
    )
    assert READS <= names
    assert "escalate_to_narada" in names


def test_prana_tier_has_full_surface(mgr):
    names = _tool_names(build_server("prana", mgr))
    assert MUTATIONS <= names
    assert READS <= names
    assert "escalate_to_narada" not in names  # prana doesn't ask itself


def test_unknown_tier_refused(mgr):
    with pytest.raises(ValueError):
        build_server("root", mgr)
