"""Tier 2: #4187 voice-input revival — the OS-invariant surfaces, not Whisper's

output. Mirrors ``tests/runtime/test_text_effect_toggle_3796.py``'s split on
purpose — two sibling opt-in features, same convention: the optional deps
(``sounddevice`` / ``faster-whisper``) are NOT reyn dependencies, so the
absent-library path is what every CI run exercises and gets a test of its
own, while anything that needs a real mic stream or a real Whisper model is
marked ``requires_voice`` and skips (this environment has neither extra).

What is deliberately NOT pinned here: transcription accuracy, Whisper's own
API surface, or anything about ``faster_whisper.WhisperModel`` — those are a
third party's promises, not reyn's (`testing.md` § "Third-party promises are
not reyn's to test").
"""
from __future__ import annotations

import pytest
from textual_flowview import FlowView

from reyn.config.media import VoiceConfig
from reyn.config.root import ReynConfig
from reyn.interfaces.inline.textual_chat import TextualChatApp, chrome, voice
from tests.interfaces.test_textual_chat_copy_rewind_3362 import (
    ScriptedTransport,
    _PickerReadModel,
    _settle,
    _texts,
)

requires_voice = pytest.mark.skipif(
    not voice.available(),
    reason="optional sounddevice/faster-whisper not installed (reyn[voice] extra)",
)


def test_unavailable_message_names_the_extra_not_the_raw_packages() -> None:
    """Tier 2: the remedy names ``reyn[voice]`` — an operator told to install
    the raw packages directly gets a working key and never learns the extra
    exists, which is the extra failing at its one job (same rationale as
    ``text_effect.unavailable_message``)."""
    msg = voice.unavailable_message()
    assert "reyn[voice]" in msg
    assert "sounddevice" not in msg
    assert "faster_whisper" not in msg
    assert "faster-whisper" not in msg


def test_reserved_keys_no_longer_claims_ctrl_r_or_f2() -> None:
    """Tier 2: #4187 landed the feature (F2, in ``app.py``'s own declarative
    ``BINDINGS`` — findable there), so the RESERVED_KEYS placeholder for it
    is gone rather than left stale. This is reyn's own dict, not a claim
    about any third party."""
    assert "f2" not in chrome.RESERVED_KEYS
    assert "ctrl+r" not in chrome.RESERVED_KEYS


@pytest.mark.asyncio
async def test_f2_binding_is_declared_and_advertised() -> None:
    """Tier 2: the declarative witness — F2 shows up on the SAME
    ``_app_binding_help`` surface the Help pane itself reads (#3818's
    single-source-of-truth convention; a hand-flattened re-read of
    ``BINDINGS`` here would miss a binding the way ``test_key_hint_wording_
    3801``'s own docstring describes). Not a claim about what pressing it
    does — that is the behavioral tests below."""
    app = TextualChatApp(transport=ScriptedTransport([]), read_model=_PickerReadModel())
    async with app.run_test():
        pairs = dict(app._app_binding_help())
        assert "f2" in pairs and "voice" in pairs["f2"].lower()
        # Ctrl+R is deliberately NOT reused (shell reverse-history-search
        # convention) — a negative control so this test isn't just "some key
        # exists somewhere".
        assert "ctrl+r" not in pairs


@pytest.mark.asyncio
async def test_the_key_says_so_when_the_library_is_absent() -> None:
    """Tier 2: with the optional deps missing, F2 reports it and names the
    install — instead of doing nothing, which reads as a broken key."""
    if voice.available():
        pytest.skip("library present; the absent path is what this pins")

    app = TextualChatApp(transport=ScriptedTransport([]), read_model=_PickerReadModel())
    async with app.run_test() as pilot:
        await pilot.pause()
        await app.action_voice_toggle()
        await pilot.pause()

        said = [t for t in _texts(app) if "voice" in t]
        assert said, f"the key did nothing visible: {_texts(app)}"
        assert any("reyn[voice]" in t for t in said), (
            f"the message names no extra to install: {said}"
        )


@pytest.mark.asyncio
async def test_disabled_in_config_short_circuits_before_touching_the_library() -> None:
    """Tier 2: ``voice.enabled: false`` reports a clear reason and never
    constructs a recorder — checked even in an environment where the
    library IS present, so this rung is provably ahead of the
    availability check, not redundant with it."""
    config = ReynConfig(voice=VoiceConfig(enabled=False))
    app = TextualChatApp(
        transport=ScriptedTransport([]), read_model=_PickerReadModel(), config=config,
    )
    async with app.run_test() as pilot:
        await pilot.pause()
        await app.action_voice_toggle()
        await pilot.pause()

        said = [t for t in _texts(app) if "voice" in t.lower()]
        assert any("enabled" in t for t in said), f"no reason given: {said}"


@requires_voice
@pytest.mark.asyncio
async def test_start_then_cancel_via_escape_reports_cancellation_not_a_transcript() -> None:
    """Tier 2: the escape-cancel rung (#4187, a new top rung on
    ``action_close_drawer``) discards an open recording without
    transcribing and never reaches the drawer/tail-jump logic below it —
    checked through the conversation pane's public text (the same surface
    every other test in this ladder uses), never ``_voice_input`` directly.

    The composer stays empty is the behavioral proof that cancel took the
    "never transcribed" branch rather than the ordinary stop-and-inject one:
    a real transcript would have landed there instead.
    """
    app = TextualChatApp(transport=ScriptedTransport([]), read_model=_PickerReadModel())
    async with app.run_test() as pilot:
        await pilot.pause()
        await app.action_voice_toggle()  # start
        await pilot.pause()
        started = [t for t in _texts(app) if "recording" in t.lower()]
        assert started, f"no start was reported: {_texts(app)}"

        app.action_close_drawer()  # escape
        await pilot.pause()

        said = [t for t in _texts(app) if "cancel" in t.lower()]
        assert said, f"no cancellation was reported: {_texts(app)}"
        assert app.query_one(chrome.Composer).text == "", (
            "the composer received text — this was a transcript, not a cancel"
        )
        # The FlowView's own overlay is untouched by this path — voice input
        # shares no state with #3796's text effect.
        assert not app.query_one(FlowView).overlay_active


@requires_voice
@pytest.mark.asyncio
async def test_max_duration_s_auto_stops_a_forgotten_recording() -> None:
    """Tier 2: ``voice.max_duration_s`` bounds a recording nobody stopped —
    the exact "declared config key, nothing consumes it" gap #4187's own
    issue body raised about the ``voice:`` block as a whole, checked here
    for the one field this module leaves easiest to silently drop (there is
    no caller-visible symptom of an unenforced timer, unlike a rejected
    config value that at least warns).

    A tiny ``max_duration_s`` (not a real recording length) makes this fast
    without a fixed sleep — ``_settle`` polls the real transcribing status
    that only ``_voice_finish_recording`` (the auto-stop path) can produce;
    NOTHING in this test calls stop/cancel itself."""
    config = ReynConfig(voice=VoiceConfig(max_duration_s=0.05))
    app = TextualChatApp(
        transport=ScriptedTransport([]), read_model=_PickerReadModel(), config=config,
    )
    async with app.run_test() as pilot:
        await pilot.pause()
        await app.action_voice_toggle()  # start — nothing stops it from here
        await pilot.pause()

        await _settle(pilot, until=lambda: any(
            "transcrib" in t.lower() for t in _texts(app)
        ))

        said = " | ".join(_texts(app))
        assert "transcrib" in said.lower(), (
            f"max_duration_s elapsed but nothing stopped the recording: {said}"
        )
