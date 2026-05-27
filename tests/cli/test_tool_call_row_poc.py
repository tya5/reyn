"""Tier 2: ToolCallRow widget shape + state transitions (issue #427 PoC).

Pins the user-visible contract:

1. Running: ``● <tool>(<args>)  · <elapsed>`` on line 1, empty line 2 until
   ``set_result`` is called.
2. ``set_result(snippet)`` populates line 2 with ``  ⎿ <snippet>``.
3. Terminal states: ``finish_success`` → ``✓``, ``finish_failure`` → ``✗``,
   ``finish_aborted`` → ``⊘``.
4. Post-terminal: ``set_result`` and further ``finish_*`` calls are
   ignored (= frozen).
5. Long args are truncated with ``…`` to fit within terminal width.

All assertions go through public render surfaces (= ``_build_line1().plain``
+ ``_build_line2().plain``) per CLAUDE.md testing policy ("NEVER assert
on private state. Use the public surface or a snapshot()-style read").

The widget is constructed without mounting it inside a Textual app —
``_refresh`` returns early when its Static children are None, so the
internal state can be driven directly against the public API and
verified via the render helpers, the same way ``test_skill_activity_plan_step_persist``
exercises ``SkillActivityRow``.
"""
from __future__ import annotations

import sys
from pathlib import Path

_SRC = Path(__file__).parent.parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))


def _row(tool_name: str = "web_fetch", args_repr: str = "url=https://example.com"):
    """Construct an unmounted ToolCallRow for direct API exercise."""
    from reyn.chat.tui.widgets.tool_call_row import ToolCallRow
    return ToolCallRow(tool_name=tool_name, args_repr=args_repr)


def test_running_line1_contains_glyph_tool_args_and_elapsed() -> None:
    """Tier 2: initial running render carries all four primary segments."""
    row = _row(tool_name="web_fetch", args_repr="url=https://example.com")
    line1 = row.render_line1().plain
    assert "●" in line1, "running state-glyph"
    assert "web_fetch" in line1, "tool name"
    assert "url=https://example.com" in line1, "args"
    assert "s" in line1, "elapsed segment (e.g. '0.0s')"


def test_line2_empty_until_set_result_called() -> None:
    """Tier 2: line 2 is empty when no result snippet has arrived yet."""
    row = _row()
    assert row.render_line2().plain == ""
    row.set_result("body length 1.2 KB")
    line2 = row.render_line2().plain
    assert "⎿" in line2, "result indent marker"
    assert "body length 1.2 KB" in line2, "snippet present"


def test_finish_success_transitions_to_check_glyph() -> None:
    """Tier 2: success terminal renders ``✓`` instead of ``●``."""
    row = _row()
    row.finish_success(result_snippet="200 OK")
    line1 = row.render_line1().plain
    assert "✓" in line1
    assert "●" not in line1
    line2 = row.render_line2().plain
    assert "200 OK" in line2


def test_finish_failure_transitions_to_cross_glyph() -> None:
    """Tier 2: failure terminal renders ``✗``."""
    row = _row()
    row.finish_failure(reason="timeout")
    line1 = row.render_line1().plain
    assert "✗" in line1
    assert "●" not in line1


def test_finish_aborted_transitions_to_circle_slash_glyph() -> None:
    """Tier 2: aborted terminal renders ``⊘`` (= distinct from ✗)."""
    row = _row()
    row.finish_aborted()
    line1 = row.render_line1().plain
    assert "⊘" in line1
    assert "✗" not in line1
    assert "●" not in line1


def test_post_terminal_set_result_and_finish_are_ignored() -> None:
    """Tier 2: once terminal, further state mutations don't change render."""
    row = _row()
    row.finish_success(result_snippet="first")
    line1_after_first = row.render_line1().plain
    line2_after_first = row.render_line2().plain
    row.set_result("second-result-should-be-ignored")
    row.finish_failure(reason="this-should-also-be-ignored")
    assert row.render_line1().plain == line1_after_first, (
        "line 1 (= state glyph + elapsed) must be frozen"
    )
    assert row.render_line2().plain == line2_after_first, (
        "line 2 (= result snippet) must be frozen"
    )
    assert "second-result-should-be-ignored" not in row.render_line2().plain
    assert "✗" not in row.render_line1().plain


def test_long_args_truncated_with_ellipsis_within_terminal_width() -> None:
    """Tier 2: args that overflow the line collapse to an ``…`` suffix.

    The widget's ``self.size.width`` is 0 (= not mounted), which the
    truncation helper handles by falling back to an 80-cell target.
    A 200-character args string at 80-cell width is guaranteed to
    require truncation regardless of margin / glyph / elapsed sizing.
    """
    long_args = "url=" + ("a" * 200)
    row = _row(tool_name="web_fetch", args_repr=long_args)
    line1 = row.render_line1().plain
    assert "…" in line1, "ellipsis appears when args overflow line width"
    # The tool name + elapsed still survive truncation (= args is the
    # disposable segment, never the framing).
    assert "web_fetch" in line1
    assert "s" in line1  # elapsed survives


def test_terminal_state_with_sub_100ms_elapsed_hides_segment() -> None:
    """Tier 2: terminal-state row with elapsed < 0.1s hides ``· 0.0s`` noise.

    F-D (wave-#427 follow-up): fast ops (= file_read cache hit / 等)
    rendered ``· 0.0s`` which is noise — sub-100ms is below display
    resolution. Hide the elapsed segment entirely when the row is
    terminal AND elapsed is below the threshold.
    """
    from reyn.chat.tui.widgets.tool_call_row import ToolCallRow
    row = ToolCallRow(tool_name="cached_op", args_repr="key=x")
    # Force frozen elapsed to a sub-threshold value before finishing
    # — avoids race against monotonic clock in test environment.
    row._frozen_elapsed = 0.0
    row.finish_success(result_snippet="ok")
    # Re-pin frozen elapsed because finish_success captures current
    # monotonic (= may have advanced past threshold during test setup).
    row._frozen_elapsed = 0.0
    line1 = row.render_line1().plain
    assert "0.0s" not in line1, "sub-100ms elapsed should be hidden in terminal state"
    # Tool name + glyph still visible.
    assert "cached_op" in line1
    assert "✓" in line1


def test_running_state_always_shows_elapsed_even_at_zero() -> None:
    """Tier 2: running rows ALWAYS show elapsed (= "alive" signal)."""
    from reyn.chat.tui.widgets.tool_call_row import ToolCallRow
    row = ToolCallRow(tool_name="slow_op", args_repr="key=x")
    # Force frozen elapsed visible by NOT finishing — still running.
    # Render and check elapsed appears regardless of value.
    line1 = row.render_line1().plain
    assert "s" in line1, "running row carries elapsed (= alive signal)"


def test_terminal_state_above_threshold_shows_elapsed() -> None:
    """Tier 2: terminal-state row with elapsed >= 0.1s still shows the
    elapsed segment (= meaningful timing preserved)."""
    from reyn.chat.tui.widgets.tool_call_row import ToolCallRow
    row = ToolCallRow(tool_name="op", args_repr="")
    row.finish_success(result_snippet="ok")
    # Frozen elapsed has whatever monotonic captured; force above threshold.
    row._frozen_elapsed = 0.5
    line1 = row.render_line1().plain
    assert "0.5s" in line1


def test_long_qualified_tool_name_middle_elides_for_args_budget() -> None:
    """Tier 2: ``mcp__server__tool_name(args)`` middle-elides when over budget.

    F-E (wave-#427 follow-up): before this fix, very long qualified
    tool names ate all of the body budget, leaving args with 0
    cells (= args fully truncated to "…"). Middle-elide keeps the
    head + tail of the qualified name so args still get a usable
    budget.
    """
    from reyn.chat.tui.widgets.tool_call_row import (
        ToolCallRow,
        _maybe_middle_elide,
    )

    # Unit test the helper directly — easier to verify shape than
    # integration through _build_line1's budget arithmetic.
    name = "mcp__some_long_server_namespace__do_the_thing_now"
    # Budget large enough for ``mcp__…__do_the_thing_now`` (= 24 cells).
    elided = _maybe_middle_elide(name, max_cells=30)
    # Head + tail preserved, middle replaced with ``…``.
    assert elided.startswith("mcp__")
    assert elided.endswith("__do_the_thing_now")
    assert "…" in elided
    # Plain (non-qualified) names fall through unchanged.
    assert _maybe_middle_elide("short_name", max_cells=20) == "short_name"
    # Short names under budget are returned verbatim.
    assert _maybe_middle_elide("a__b__c", max_cells=100) == "a__b__c"
    # Two-segment names (= no middle to elide) also fall through.
    assert _maybe_middle_elide("only__two", max_cells=5) == "only__two"
    # When even the elided form exceeds the budget, helper bails to the
    # original name (caller's tail-truncate then takes over).
    assert _maybe_middle_elide(name, max_cells=10) == name

    # Integration: ToolCallRow with a very long qualified name still
    # surfaces both prefix and suffix of the name in line 1.
    row = ToolCallRow(
        tool_name=name, args_repr="param1=value1",
    )
    line1 = row.render_line1().plain
    assert "mcp" in line1, "prefix segment of qualified name survives"
    assert "do_the_thing_now" in line1, "tail segment of qualified name survives"


def test_result_snippet_truncated_when_long() -> None:
    """Tier 2: long result snippets get the same ``…`` treatment on line 2."""
    row = _row()
    long_result = "x" * 200
    row.set_result(long_result)
    line2 = row.render_line2().plain
    assert "…" in line2
    assert "⎿" in line2  # indent marker preserved


def test_failed_terminal_surfaces_reason_on_line2() -> None:
    """Tier 2: ``finish_failure(reason=...)`` renders the reason on line 2.

    F-B (wave-#427 follow-up): before this contract, a failed tool
    call showed ``✗ tool(args) · 0.5s`` with empty line 2 — the user
    had to switch to the right-panel events tab to see *why* the
    call failed. Now the reason is inline so the conv pane carries
    a complete failure narrative.
    """
    row = _row()
    row.finish_failure(reason="timeout")
    line2 = row.render_line2().plain
    assert "⎿" in line2, "indent marker preserved"
    assert "✗" in line2, "failure glyph mirrors line 1"
    assert "timeout" in line2, "reason text visible"


def test_aborted_terminal_surfaces_reason_with_circle_slash_on_line2() -> None:
    """Tier 2: aborted state uses ``⊘`` glyph on line 2 (matching line 1)."""
    row = _row()
    row.finish_aborted(reason="user cancelled")
    line2 = row.render_line2().plain
    assert "⎿" in line2
    assert "⊘" in line2, "aborted glyph mirrors line 1"
    assert "user cancelled" in line2


def test_failed_with_explicit_result_snippet_prefers_result() -> None:
    """Tier 2: when both result_snippet and reason are present, result wins.

    The hypothetical case (= a tool that fails but still returns a
    structured payload). Result snippet is the more specific signal.
    """
    row = _row()
    row.finish_failure(reason="non-zero exit", result_snippet="status=err, exit_code=1")
    line2 = row.render_line2().plain
    assert "status=err" in line2
    # The reason text is NOT shown in this case — result snippet wins.
    assert "non-zero exit" not in line2


def test_failed_with_empty_reason_omits_line2() -> None:
    """Tier 2: failure with no reason text → still empty line 2 (no orphan ✗)."""
    row = _row()
    row.finish_failure(reason="")
    line2 = row.render_line2().plain
    assert line2 == ""
