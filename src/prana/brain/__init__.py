"""prana.brain — the warm brain server.

Narada-the-agent exposed as an OpenAI-compatible endpoint
(``/v1/chat/completions``, streaming). One warm agent client per
session, bearer-token auth with caller tiers, a frozen turn-lifecycle
contract. Spec: docs/plans/personal-agent-platform-2026-09-04.md §1a.
"""
