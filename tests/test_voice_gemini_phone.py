"""The Gemini phone call's lean prompt (Suti 2026-11-05: trim the voice
instructions; Narada pulls memories and today's log with tools)."""
import pytest

pytest.importorskip("livekit.plugins.google")

from prana.voice.phone_worker import GEMINI_PHONE_TOOLS, GEMINI_TODAY_NOTE, phone_tools, tool_name  # noqa: E402
from prana.voice.tools import build_voice_tools  # noqa: E402


async def _noop(*a, **k):
    return None


def _tools():
    return build_voice_tools(tier="personal", session_id="x", music=None, publish=_noop,
                             end_session=lambda: None, speak=_noop, voice_info={})


def test_every_allowlisted_tool_exists():
    """A renamed tool must fail here, not silently vanish from calls."""
    names = {tool_name(t) for t in _tools()}
    assert GEMINI_PHONE_TOOLS <= names, GEMINI_PHONE_TOOLS - names


def test_gemini_gets_the_call_sized_set_and_openai_everything():
    tools = _tools()
    gemini = {tool_name(t) for t in phone_tools(tools, gemini=True)}
    assert gemini == GEMINI_PHONE_TOOLS
    assert {"recall_memory", "log_meal", "get_today_summary", "escalate_to_narada"} <= gemini
    # the BOX's media and the call-blocking session action stay off the phone
    assert not gemini & {"cast_music", "tv_mode", "set_volume", "play_music", "request_session_action"}
    assert len(phone_tools(tools, gemini=False)) == len(tools)


def test_the_today_note_points_at_the_tool_it_names():
    assert "get_today_summary" in GEMINI_TODAY_NOTE
    assert "get_today_summary" in GEMINI_PHONE_TOOLS
