"""Tier 2: inline CC-style renderer — kind→marker+text contract + factory wiring.

The inline renderer is the default interactive `reyn chat` surface after the
Textual cutover. Each message renders as a 2-cell marker gutter + a wrapping body
column (the agent body as markdown); these assert the rendered-text contract
(markers present, text preserved, meta prefix applied, gutter reserved on wrap,
markdown parsed) — not exact whitespace, so formatting tweaks don't break them.
"""
from __future__ import annotations

import io

from rich.console import Console

from reyn.interfaces.cli.logger_factory import (
    make_chat_renderer,
    make_inline_renderer,
)
from reyn.interfaces.repl.renderer import (
    ChatRenderer,
    ConsoleChatRenderer,
    InlineChatRenderer,
    _harden_soft_breaks,
    format_inline_message,
    wants_separator,
)
from reyn.runtime.outbox import OutboxMessage


def _plain(kind: str, text: str, meta: dict | None = None, *, width: int = 80) -> str:
    """Render a message to plain text. The renderable is now a gutter grid (not a
    bare Text), so we render it through a Console to assert the marker/body
    contract on the visible output."""
    console = Console(width=width, file=io.StringIO(), color_system=None)
    console.print(format_inline_message(OutboxMessage(kind=kind, text=text, meta=meta or {})))
    return console.file.getvalue()


def _render_ansi(kind: str, text: str, *, width: int = 30) -> str:
    """Render with truecolor ANSI on, to assert styling (e.g. the user-input
    background block emits a ``48;2;`` background SGR)."""
    console = Console(width=width, file=io.StringIO(), force_terminal=True,
                      color_system="truecolor")
    console.print(format_inline_message(OutboxMessage(kind=kind, text=text)))
    return console.file.getvalue()


def test_agent_body_renders_markdown_not_raw_source() -> None:
    """Tier 2: the agent (LLM) body renders as markdown — **bold** becomes styled
    text (the ** source is consumed) and list items become bullets, like CC."""
    out = _plain("agent", "Some **bold** words:\n- one\n- two")
    assert "**" not in out          # markdown parsed, not shown as raw source
    assert "bold" in out
    assert "one" in out and "two" in out
    assert "•" in out               # list rendered as bullets


def test_wrapped_agent_body_hang_indents_clear_of_the_gutter() -> None:
    """Tier 2: a wrapped body continues INDENTED in the body column, never bleeding
    back into the 2-cell marker gutter (the reserved-gutter contract)."""
    out = _plain("agent", "word " * 40, width=40)
    lines = [ln for ln in out.split("\n") if ln.strip()]
    # a wrapped continuation line exists, indented into the body column with no
    # marker → the long body did wrap AND hang-indented clear of the gutter
    assert any(c.startswith("  ") and "⏺" not in c for c in lines)
    # the marker only ever LEADS a line (it never appears inside the body / in the
    # gutter of a continuation line)
    assert all(("⏺" not in c) or c.startswith("⏺") for c in lines)


def test_long_unbreakable_token_folds_instead_of_truncating() -> None:
    """Tier 2: a long unbreakable token (path / identifier / URL) folds onto a
    continuation line rather than being cropped at the right edge with an ellipsis.
    rich Table columns default to overflow='ellipsis'; the body column overrides to
    'fold' so the whole token survives — its tail is still present and no '…' crop
    marker appears."""
    token = "/x/" + "segment_" * 12 + "TOKENTAIL"
    out = _plain("agent", f"Path: {token}", width=40)
    assert "…" not in out                 # not ellipsis-truncated
    assert "TOKENTAIL" in out             # the token tail survived the fold


def test_user_line_carries_a_background_block() -> None:
    """Tier 2: the user's own line gets a background block (CC-style 'you said
    this' design); the plain agent line does not."""
    assert "48;2;" in _render_ansi("user", "my message")      # bg SGR present
    assert "48;2;" not in _render_ansi("agent", "plain reply")  # none on agent


def test_user_echo_leads_with_input_marker_and_keeps_text() -> None:
    """Tier 2: the user's own submitted line is echoed with the ❯ input marker
    (same chevron CC uses for the prompt) and its text — so the message stays
    visible in the conversation after the inline input field clears on submit."""
    out = _plain("user", "what files changed?")
    assert "❯" in out
    assert "what files changed?" in out


def test_agent_line_leads_with_dot_marker_and_keeps_text() -> None:
    """Tier 2: an agent message renders with the ⏺ marker and its text."""
    out = _plain("agent", "hello world")
    assert "⏺" in out
    assert "hello world" in out


def test_error_line_uses_cross_marker() -> None:
    """Tier 2: an error renders with the ✗ marker and its text."""
    out = _plain("error", "boom")
    assert "✗" in out
    assert "boom" in out


def test_trace_line_uses_corner_marker() -> None:
    """Tier 2: a trace/detail line renders under the ⎿ marker."""
    out = _plain("trace", "phase started")
    assert "⎿" in out
    assert "phase started" in out


def test_skill_done_leads_with_check_marker() -> None:
    """Tier 2: a finished skill leads with the ✓ completion marker (distinct from
    the ⏺ assistant marker), and keeps its text."""
    out = _plain("skill_done", "skill finished")
    assert "✓" in out
    assert "skill finished" in out


def test_intervention_keeps_question_text() -> None:
    """Tier 2: an intervention line preserves the question text."""
    out = _plain("intervention", "Which file?")
    assert "Which file?" in out


def test_intervention_suppresses_run_id_short_prefix() -> None:
    """Tier 2: intervention drops the [#short] run-id hash — it is cryptic noise on
    an interactive user-facing prompt where disambiguation is unnecessary (#2243)."""
    out = _plain("intervention", "Confirm?", meta={"run_id_short": "ab12"})
    assert "#ab12" not in out
    assert "Confirm?" in out


def test_intervention_keeps_skill_name_prefix_when_present() -> None:
    """Tier 2: skill_name context is retained on interventions so the user sees which
    skill is asking — only run_id_short (the cryptic hash) is suppressed."""
    out = _plain(
        "intervention", "Confirm?",
        meta={"skill_name": "skill_builder", "run_id_short": "ab12"},
    )
    assert "skill_builder" in out
    assert "#ab12" not in out
    assert "Confirm?" in out


def test_kinds_use_distinct_markers() -> None:
    """Tier 2: message kinds carry distinct glyphs so the eye separates them — the
    assistant ⏺ is not reused for an intervention (◆), a finished skill (✓), or a
    tool invocation (▸)."""
    agent = _plain("agent", "x")
    interv = _plain("intervention", "x")
    done = _plain("skill_done", "x")
    tool = _plain("tool_call_started", "", {"tool": "Bash", "args": {}})
    assert "⏺" in agent
    assert "◆" in interv and "⏺" not in interv
    assert "✓" in done and "⏺" not in done
    assert "▸" in tool and "⏺" not in tool


def test_tool_call_completed_renders_corner_marker_and_summary() -> None:
    """Tier 2: a tool_call_completed result renders with the ⎿ nested marker
    and the summarize_tool_result one-liner (e.g. 'Read 3 lines')."""
    out = _plain(
        "tool_call_completed", "",
        meta={"tool": "file__read", "result": {"op": "read", "status": "ok", "content": "a\nb\nc"}},
    )
    assert "⎿" in out
    assert "Read 3 lines" in out


def test_tool_call_failed_renders_corner_marker_and_error() -> None:
    """Tier 2: a tool_call_failed result renders with the ⎿ nested marker and
    the error text, prefixed with ✗."""
    out = _plain(
        "tool_call_failed", "",
        meta={"error_message": "file not found"},
    )
    assert "⎿" in out
    assert "✗" in out
    assert "file not found" in out


def test_wants_separator_skips_first_nested_and_transient() -> None:
    """Tier 2: a blank line separates top-level message blocks (but not before the
    first); never before a nested ⎿ detail row (tool result), nor before a
    TRANSIENT status/trace — a transient is cleared in place, so a separator before
    it would orphan as a stray blank (the old 2-blank-before-reply bug)."""
    assert wants_separator("agent", seen_message=False) is False           # first
    assert wants_separator("agent", seen_message=True) is True             # block gap
    assert wants_separator("tool_call_completed", seen_message=True) is False  # nested
    assert wants_separator("tool_call_failed", seen_message=True) is False     # nested
    assert wants_separator("status", seen_message=True) is False           # transient
    assert wants_separator("trace", seen_message=True) is False            # transient+nested


def test_system_lifecycle_marker_has_dim_gutter() -> None:
    """Tier 2: system kind (compaction / budget-warn / cost-warn markers) renders
    with a dim '· ' gutter instead of falling through to raw unstyled plain text.
    Compaction, budget, and cost-warn all use kind='system'; without the _KIND_LINE
    entry they rendered identically to unknown kinds — no visual hierarchy."""
    out = _plain("system", "[↑ 5 turns compacted]")
    assert "·" in out
    assert "[↑ 5 turns compacted]" in out


def test_unknown_kind_renders_text_without_marker() -> None:
    """Tier 2: an unrecognised kind falls back to plain text (no kind marker)."""
    out = _plain("totally_new_kind", "raw payload")
    assert out.strip() == "raw payload"


def test_meta_skill_prefix_is_applied() -> None:
    """Tier 2: skill_name + run_id_short surface as a [skill#abcd] prefix."""
    out = _plain(
        "agent", "done",
        meta={"skill_name": "skill_builder", "run_id_short": "ab12"},
    )
    assert "[skill_builder#ab12]" in out
    assert "done" in out


def test_inline_factory_returns_inline_renderer() -> None:
    """Tier 2: make_inline_renderer() builds the inline (default TTY) renderer."""
    r = make_inline_renderer()
    assert isinstance(r, InlineChatRenderer)
    assert isinstance(r, ChatRenderer)


def test_plain_factory_returns_console_renderer() -> None:
    """Tier 2: make_chat_renderer() still builds the plain --cui renderer."""
    r = make_chat_renderer()
    assert isinstance(r, ConsoleChatRenderer)
    assert isinstance(r, ChatRenderer)


# ---------------------------------------------------------------------------
# ConsoleChatRenderer working indicator (#2269 fix)
# ---------------------------------------------------------------------------


class _Ev:
    def __init__(self, type_: str) -> None:
        self.type = type_


def test_console_renderer_bottom_toolbar_none_at_init() -> None:
    """Tier 2: bottom_toolbar() returns None when no turn is in flight (default state)."""
    r = ConsoleChatRenderer()
    assert r.bottom_toolbar() is None


def test_console_renderer_bottom_toolbar_non_none_after_turn_started() -> None:
    """Tier 2: on_chat_event(turn_started) → bottom_toolbar() returns a non-None
    working indicator string (the in-flight cue #2268 removed)."""
    r = ConsoleChatRenderer()
    r.on_chat_event(_Ev("turn_started"))
    result = r.bottom_toolbar()
    assert result is not None
    assert "working" in result


def test_console_renderer_bottom_toolbar_clears_on_turn_settled() -> None:
    """Tier 2: on_chat_event(turn_settled) after turn_started → bottom_toolbar() is None."""
    r = ConsoleChatRenderer()
    r.on_chat_event(_Ev("turn_started"))
    r.on_chat_event(_Ev("turn_settled"))
    assert r.bottom_toolbar() is None


def test_console_renderer_bottom_toolbar_clears_on_turn_completed() -> None:
    """Tier 2: on_chat_event(turn_completed) after turn_started → bottom_toolbar() is None."""
    r = ConsoleChatRenderer()
    r.on_chat_event(_Ev("turn_started"))
    r.on_chat_event(_Ev("turn_completed"))
    assert r.bottom_toolbar() is None


def test_console_renderer_bottom_toolbar_clears_on_turn_cancelled() -> None:
    """Tier 2: on_chat_event(turn_cancelled) after turn_started → bottom_toolbar() is None."""
    r = ConsoleChatRenderer()
    r.on_chat_event(_Ev("turn_started"))
    r.on_chat_event(_Ev("turn_cancelled"))
    assert r.bottom_toolbar() is None


# ---------------------------------------------------------------------------
# _harden_soft_breaks — single-newline preservation for agent output
# ---------------------------------------------------------------------------


def test_harden_soft_breaks_adds_trailing_spaces_to_paragraph_lines() -> None:
    """Tier 2: _harden_soft_breaks appends two spaces to non-structural paragraph
    lines so CommonMark treats the following newline as a hard line break instead
    of collapsing it to a space."""
    result = _harden_soft_breaks("first\nsecond\nthird")
    # Each non-last paragraph line gains two trailing spaces.
    assert result == "first  \nsecond  \nthird"


def test_harden_soft_breaks_leaves_structural_lines_untouched() -> None:
    """Tier 2: heading, list-item, blank-line, and code-fence lines must NOT gain
    trailing spaces — the markdown parser relies on their raw newlines to identify
    the element type.

    Also: the LAST line of any input never gains trailing spaces (no newline
    follows it, so there is nothing to harden).
    """
    # heading followed by text: heading is structural; "some text" is the last
    # line, so no trailing spaces are added to either.
    assert _harden_soft_breaks("# Title\nsome text") == "# Title\nsome text"
    # unordered list items — list markers are structural, no trailing spaces.
    assert _harden_soft_breaks("- alpha\n- beta") == "- alpha\n- beta"
    # blank line between paragraphs: blank is structural, so para1's next is
    # structural → no hardening. para2 is last → no hardening.
    assert _harden_soft_breaks("para1\n\npara2") == "para1\n\npara2"
    # code fence delimiter is structural; "code" sits between two structural
    # lines, so it also stays untouched.
    assert _harden_soft_breaks("```\ncode\n```") == "```\ncode\n```"


def test_agent_single_newlines_appear_on_separate_lines() -> None:
    """Tier 2: agent messages with single-newline-separated lines render on distinct
    output lines — not collapsed to 'first second third' by the markdown renderer."""
    out = _plain("agent", "first\nsecond\nthird")
    output_lines = [ln for ln in out.splitlines() if ln.strip()]
    # Each word must appear in a different line (not all on one).
    first_line = next((ln for ln in output_lines if "first" in ln), None)
    second_line = next((ln for ln in output_lines if "second" in ln), None)
    assert first_line is not None and second_line is not None
    assert first_line != second_line, (
        f"'first' and 'second' collapsed to the same line; output:\n{out}"
    )


def test_agent_markdown_list_items_still_render_as_bullets() -> None:
    """Tier 2: markdown list items (- item) are NOT affected by _harden_soft_breaks
    and continue to render as bullet points — structural lines are exempt from
    the soft-break hardening so the markdown parser still recognises them."""
    out = _plain("agent", "- alpha\n- beta\n- gamma")
    assert "•" in out
    assert "alpha" in out and "beta" in out and "gamma" in out
