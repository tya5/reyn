"""Tier 2: inline app input driver — working-row fragments + input-path routing flag.

The Application itself is an interactive driver verified live (e2e); here we pin
the pure fragment builder and the renderer capability flag that selects the app
input path. Assertions are on public return values, not whitespace/private state.
"""
from __future__ import annotations

import time
from datetime import datetime, timezone

from reyn.interfaces.inline.app import working_line
from reyn.interfaces.repl.renderer import (
    ChatRenderer,
    ConsoleChatRenderer,
    InlineChatRenderer,
)
from reyn.schemas.models import Event


def _evt(t: str, **data) -> Event:
    return Event(type=t, timestamp=datetime.now(timezone.utc), data=data)


def test_working_line_idle_is_empty() -> None:
    """Tier 2: no working row when a turn is not running."""
    assert working_line(False, 0.0, 5.0) == []


def test_working_line_running_has_spinner_and_label() -> None:
    """Tier 2: while running (no waiting_on given → the default), the row
    carries a spinner glyph + 'Thinking…' (owner: renamed from "Working" —
    "LLM 処理中は Thinking の方が良さそう")."""
    frags = working_line(True, 0.0, 3.0)
    text = "".join(t for _, t in frags)
    assert "Thinking" in text
    assert "3s" in text          # elapsed = now - start
    assert text.strip()[0] not in ("T",)  # a spinner glyph leads, not the label


def test_working_line_elapsed_tracks_now_minus_start() -> None:
    """Tier 2: elapsed seconds = floor(now - think_start)."""
    text = "".join(t for _, t in working_line(True, 10.0, 17.4))
    assert "7s" in text


def test_working_line_never_negative_elapsed() -> None:
    """Tier 2: a clock skew (now < start) clamps elapsed to 0, not negative."""
    text = "".join(t for _, t in working_line(True, 10.0, 9.0))
    assert "0s" in text
    assert "Working… -" not in text  # no negative sign before elapsed seconds


def test_working_line_has_a_moving_shimmer_crest() -> None:
    """Tier 2: the label carries a bright shimmer crest whose position sweeps with
    the clock (it animates), not a static dim line."""
    def crest_char(now: float):
        # The crest is the lone bold fragment among the label characters.
        for style, text in working_line(True, 0.0, now):
            if "bold" in style and text.strip():
                return text
        return None
    c0 = crest_char(0.0)    # head at char 0
    c1 = crest_char(0.10)   # head advances → crest on a later char
    assert c0 is not None and c1 is not None  # a crest exists (shimmer present)
    assert c0 != c1                            # and it moved (animated)


def test_inline_renderer_selects_app_input() -> None:
    """Tier 2: the interactive inline renderer drives input via its own app."""
    assert InlineChatRenderer().uses_app_input() is True


def test_plain_renderers_keep_promptsession_path() -> None:
    """Tier 2: plain / base renderers stay on the PromptSession _input_loop."""
    assert ConsoleChatRenderer().uses_app_input() is False
    assert ChatRenderer().uses_app_input() is False


def test_turn_settled_clears_indicator_after_short_circuit_turn() -> None:
    """Tier 2: turn_settled clears the working indicator even when no
    turn_completed fired (slash / intervention short-circuit paths)."""
    r = InlineChatRenderer()
    r.on_chat_event(_evt("turn_started"))
    assert r.bottom_toolbar() is not None
    # A slash turn ends with turn_settled (no turn_completed) — must clear.
    r.on_chat_event(_evt("turn_settled"))
    assert r.bottom_toolbar() is None


# ── working_line cancelling-state tests ─────────────────────────────────────


def test_working_line_normal_shows_interrupt_affordance() -> None:
    """Tier 2: normal working row includes 'ctrl-c to interrupt' hint."""
    text = "".join(t for _, t in working_line(True, 0.0, 3.0))
    assert "ctrl-c" in text


def test_working_line_cancelling_shows_cancelling_text() -> None:
    """Tier 2: when cancelling=True the row shows 'Cancelling' not the shimmer."""
    frags = working_line(True, 0.0, 3.0, cancelling=True)
    text = "".join(t for _, t in frags)
    assert "Cancelling" in text
    # Shimmer elements gone: no spinner, no elapsed seconds.
    assert "Working" not in text
    assert "ctrl-c" not in text


def test_working_line_idle_cancelling_is_empty() -> None:
    """Tier 2: idle (thinking=False) returns [] even when cancelling=True."""
    assert working_line(False, 0.0, 3.0, cancelling=True) == []


def test_cancelling_state_does_not_bleed_into_next_turn() -> None:
    """Tier 2: a ctrl-c cancel in one turn does not show 'Cancelling…' in the next.

    The ConditionalContainer hides the working row when _thinking=False, so the old
    clear-in-_working_frags path was dead code. on_chat_event must reset the flag on
    turn end so it never leaks. Verified via InlineChatRenderer.working_frags() —
    the same public surface the app drives; no private state is read in setup or
    assertion.
    """
    for end_event in ("turn_settled", "turn_completed", "turn_cancelled"):
        r = InlineChatRenderer()
        r.on_chat_event(_evt("turn_started"))
        r.request_cancel()           # public API: simulate user pressing ctrl-c mid-turn
        r.on_chat_event(_evt(end_event))
        r.on_chat_event(_evt("turn_started"))   # next turn begins
        text = "".join(t for _, t in r.working_frags(time.monotonic()))
        assert "Cancelling" not in text, (
            f"after {end_event}, next turn still shows Cancelling indicator"
        )
        assert "Thinking" in text, (
            f"after {end_event}, next turn should show the default Thinking indicator"
        )


# ── WaitingOn framework (owner: "Working… もっと状態細分化できないの?" →
# "何に待たされているのか知りたい") ────────────────────────────────────────────


def test_working_line_waiting_on_running_shows_tool_name():
    """Tier 2: a WaitingOn with a detail (tool name) renders "Running <tool>…",
    not the generic "Thinking…" default."""
    from reyn.interfaces.inline.app import WaitingOn
    text = "".join(
        t for _, t in working_line(True, 0.0, 3.0, waiting_on=WaitingOn(label="Running", detail="edit_file"))
    )
    assert "Running edit_file…" in text
    assert "Thinking" not in text


def test_working_line_waiting_on_since_resets_elapsed_not_turn_total():
    """Tier 2: elapsed seconds shown is time-in-THIS-state (waiting_on_since),
    not turn-total (think_start) — "Running grep_files… 5s" must answer "how
    long has THIS been running", not "how long has the whole turn been"."""
    from reyn.interfaces.inline.app import WaitingOn
    text = "".join(t for _, t in working_line(
        True, think_start=0.0, now=20.0,
        waiting_on=WaitingOn(label="Running", detail="grep_files"),
        waiting_on_since=15.0,  # this tool started at t=15, turn started at t=0
    ))
    assert "5s" in text   # 20 - 15, NOT 20 - 0
    assert "20s" not in text


def test_working_line_waiting_on_since_defaults_to_think_start():
    """Tier 2: omitting waiting_on_since falls back to think_start (turn-total
    elapsed) — the default/"Thinking" state has no separate sub-state start."""
    from reyn.interfaces.inline.app import WaitingOn
    text = "".join(t for _, t in working_line(
        True, think_start=10.0, now=17.0, waiting_on=WaitingOn(label="Thinking"),
    ))
    assert "7s" in text


def test_working_line_user_wait_is_static_not_shimmering():
    """Tier 2: is_user_wait=True renders a static amber row with no per-character
    shimmer crest — the "ball is in your court" state must look visually
    distinct from "the AI is busy", which was the owner's original complaint
    (the spinner kept ticking through an ask_user pause). Verified by content
    (no bold-crest fragment, unlike the shimmering "Thinking"/"Running" case
    covered by test_working_line_has_a_moving_shimmer_crest above), not by
    pinning how many fragments compose the row."""
    from reyn.interfaces.inline.app import WaitingOn
    frags = working_line(
        True, 0.0, 3.0, waiting_on=WaitingOn(label="Waiting for you", is_user_wait=True),
    )
    text = "".join(t for _, t in frags)
    assert "Waiting for you" in text
    assert "3s" in text
    assert not any("bold" in style for style, _ in frags), (
        "no shimmer crest fragment — the user-wait row must not animate"
    )


def test_working_line_cancelling_overrides_waiting_on():
    """Tier 2: cancelling=True still shows "Cancelling…" even with a
    waiting_on set (e.g. mid-tool-call) — cancel-in-progress always wins."""
    from reyn.interfaces.inline.app import WaitingOn
    text = "".join(t for _, t in working_line(
        True, 0.0, 3.0, cancelling=True, waiting_on=WaitingOn(label="Running", detail="shell"),
    ))
    assert "Cancelling" in text
    assert "Running shell" not in text


# ── InlineChatRenderer WaitingOn state-transition wiring ────────────────────


def test_tool_called_sets_running_state():
    """Tier 2: a "tool_called" chat event (the SAME event lifecycle_forwarder.py
    already consumes to render the scrollback's "▸ tool(...)" trace line —
    this is a second, independent subscriber, not new plumbing) makes the
    working row name that tool."""
    r = InlineChatRenderer()
    r.on_chat_event(_evt("turn_started"))
    r.on_chat_event(_evt("tool_called", tool="edit_file"))
    text = "".join(t for _, t in r.working_frags(time.monotonic()))
    assert "Running edit_file…" in text


def test_tool_returned_resets_to_thinking():
    """Tier 2: "tool_returned" clears the tool name — falls back to
    "Thinking…" while the LLM processes the tool's result."""
    r = InlineChatRenderer()
    r.on_chat_event(_evt("turn_started"))
    r.on_chat_event(_evt("tool_called", tool="edit_file"))
    r.on_chat_event(_evt("tool_returned", tool="edit_file"))
    text = "".join(t for _, t in r.working_frags(time.monotonic()))
    assert "Thinking" in text
    assert "Running" not in text


def test_tool_failed_resets_to_thinking():
    """Tier 2: "tool_failed" also clears the tool name (mirrors tool_returned
    — the dispatch attempt is over either way)."""
    r = InlineChatRenderer()
    r.on_chat_event(_evt("turn_started"))
    r.on_chat_event(_evt("tool_called", tool="shell"))
    r.on_chat_event(_evt("tool_failed", tool="shell"))
    text = "".join(t for _, t in r.working_frags(time.monotonic()))
    assert "Thinking" in text
    assert "Running" not in text


def test_second_tool_call_replaces_first_in_same_turn():
    """Tier 2: a turn with multiple sequential tool calls (#2344's owner
    design decision: chat-axis tool_calls run SERIALLY, never parallelized —
    verified in router_loop.py's SchemeOps.dispatch) shows whichever tool is
    CURRENTLY dispatched, not the first one from earlier in the turn."""
    r = InlineChatRenderer()
    r.on_chat_event(_evt("turn_started"))
    r.on_chat_event(_evt("tool_called", tool="read_file"))
    r.on_chat_event(_evt("tool_returned", tool="read_file"))
    r.on_chat_event(_evt("tool_called", tool="edit_file"))
    text = "".join(t for _, t in r.working_frags(time.monotonic()))
    assert "Running edit_file…" in text
    assert "read_file" not in text


def test_intervention_message_sets_waiting_for_you():
    """Tier 2: an outbox kind="intervention" message (InterventionHandler
    .announce() — the ONE signal common to ALL 6 intervention_bus.request()
    callers: ask_user / permission confirm / cost-warn / safety-limit
    checkpoint / MCP install confirm / hook confirm, verified only ask_user.py
    emits user_intervention_requested directly, so THIS is the correct
    chokepoint) switches the working row to "Waiting for you"."""
    from reyn.runtime.outbox import OutboxMessage
    r = InlineChatRenderer()
    r.on_chat_event(_evt("turn_started"))
    r.message(OutboxMessage(kind="intervention", text="Continue?"))
    text = "".join(t for _, t in r.working_frags(time.monotonic()))
    assert "Waiting for you" in text


def test_user_answered_intervention_resets_to_thinking():
    """Tier 2: "user_answered_intervention" (InterventionHandler.record_answer
    — also common to all 6 intervention paths) clears the user-wait state."""
    from reyn.runtime.outbox import OutboxMessage
    r = InlineChatRenderer()
    r.on_chat_event(_evt("turn_started"))
    r.message(OutboxMessage(kind="intervention", text="Continue?"))
    r.on_chat_event(_evt("user_answered_intervention"))
    text = "".join(t for _, t in r.working_frags(time.monotonic()))
    assert "Thinking" in text
    assert "Waiting for you" not in text


def test_waiting_on_does_not_leak_into_next_turn():
    """Tier 2: turn_started resets waiting_on to the default — a tool (or
    user-wait) from a PRIOR turn must not still show as active once a new
    turn begins (mirrors the existing cancelling-state non-bleed guard)."""
    r = InlineChatRenderer()
    r.on_chat_event(_evt("turn_started"))
    r.on_chat_event(_evt("tool_called", tool="read_file"))
    r.on_chat_event(_evt("turn_settled"))
    r.on_chat_event(_evt("turn_started"))   # next turn, no tool_called yet
    text = "".join(t for _, t in r.working_frags(time.monotonic()))
    assert "Thinking" in text
    assert "Running" not in text


def test_waiting_on_since_updates_on_each_transition(monkeypatch):
    """Tier 2: each WaitingOn transition stamps a fresh `_waiting_on_since` —
    elapsed shown after a transition is time-since-THAT-transition, not
    time-since-turn-start (the actual "how long has the CURRENT thing been
    stuck" the owner asked for)."""
    import reyn.interfaces.repl.renderer as renderer_mod
    fake_now = {"t": 100.0}
    monkeypatch.setattr(renderer_mod.time, "monotonic", lambda: fake_now["t"])

    r = InlineChatRenderer()
    r.on_chat_event(_evt("turn_started"))       # think_start = 100
    fake_now["t"] = 110.0
    r.on_chat_event(_evt("tool_called", tool="grep_files"))  # waiting_on_since = 110
    fake_now["t"] = 115.0                        # 5s into the tool call, 15s into the turn

    text = "".join(t for _, t in r.working_frags(115.0))
    assert "5s" in text
    assert "15s" not in text
