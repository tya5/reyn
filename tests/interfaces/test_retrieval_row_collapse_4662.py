"""#4662 — a settled retrieval tool call's row folds to ONE dim line by
default (#3329's deferred body-treatment half).

Measured before building (#4662's own comment thread): the "展開可"
(expandable) half of #3329's original table cell was ALREADY satisfied by
#3508's existing highlight-expand mechanism, which fires for every settled
tool row regardless of op-class — no new operation, no new keybinding.
This PR's own scope is therefore narrow: change ONLY the COLLAPSED,
unhighlighted default from a two-line ``tool(args)`` header + ``⎿ summary``
result to ONE dim ``tool(args) → summary`` line, for a retrieval tool
(#3329's ``_is_retrieval_tool``) that is settled, not currently expanded,
and did not fail.

Asserted on the PAINTED body via the presenter, mirroring
``test_tool_detail_on_highlight_3508.py``'s own discipline — the meta flag
is an implementation detail, not the thing that reaches the screen.
"""
from __future__ import annotations

from reyn.interfaces.inline.textual_chat._meta_keys import (
    EXPANDED_KEY,
    RESULT_KIND_KEY,
    RESULT_META_KEY,
)
from reyn.interfaces.inline.textual_chat.presenter import _body_and_background
from reyn.runtime.outbox import OutboxMessage


def _settled_tool(
    result: object, tool: str, *, expanded: bool = False, failed: bool = False,
) -> OutboxMessage:
    meta = {
        "tool": tool,
        "args": {"path": "a.py"},
        RESULT_KIND_KEY: "tool_call_failed" if failed else "tool_call_completed",
        RESULT_META_KEY: {"error_message": result} if failed else {"result": result},
    }
    if expanded:
        meta[EXPANDED_KEY] = True
    return OutboxMessage(kind="tool_call_started", text=tool, meta=meta)


def _plain(renderable) -> str:
    from rich.console import Console

    console = Console(width=120, file=None, record=True)
    console.begin_capture()
    console.print(renderable)
    return console.end_capture()


def test_a_settled_retrieval_call_renders_one_dim_line() -> None:
    """Tier 1: a settled, non-expanded ``read_file`` (purity=read_only, #3329)
    call folds its header and result summary together — the ``⎿`` nested
    result marker (the visual signal of a SEPARATE result line under the
    header, #3283's own coalesce vocabulary) is gone, and the header + the
    result summary both appear on the SAME line as each other (not the
    header on one line and the summary on the next)."""
    msg = _settled_tool(["line one", "line two", "line three"], "read_file")
    body, _ = _body_and_background(msg, now=None, image_cache=None, decoded_image_cache=None)

    text = _plain(body)
    assert "⎿" not in text
    header_line = next(l for l in text.splitlines() if "read_file" in l)
    assert "3 items" in header_line, (
        f"the result summary is not on the same line as the header: {text!r}"
    )


def test_a_settled_side_effect_call_keeps_the_two_line_form() -> None:
    """Tier 1: accept-side — a settled ``write_file`` (purity=side_effect)
    call is UNAFFECTED — #4662 only changes retrieval rows. Its ``⎿``
    nested-result marker (the header/result separation #3283 established)
    still appears, on a line that does NOT also carry the header text."""
    msg = _settled_tool("ok", "write_file")
    body, _ = _body_and_background(msg, now=None, image_cache=None, decoded_image_cache=None)

    text = _plain(body)
    assert "⎿" in text
    result_line = next(l for l in text.splitlines() if "⎿" in l)
    assert "write_file" not in result_line, (
        f"the header and result collapsed onto one line for a side-effect "
        f"tool — #4662 must not touch this case: {text!r}"
    )


def test_an_expanded_retrieval_call_keeps_the_two_line_expanded_form() -> None:
    """Tier 1: #3508's highlight-expand mechanism is untouched — an EXPANDED
    retrieval row (the highlight is on it) still shows the full detail via
    the ordinary two-line form, never the one-line collapse."""
    msg = _settled_tool(["line one", "line two"], "read_file", expanded=True)
    body, _ = _body_and_background(msg, now=None, image_cache=None, decoded_image_cache=None)

    text = _plain(body)
    assert "line one" in text and "line two" in text
    assert "⎿" in text


def test_a_failed_retrieval_call_keeps_the_two_line_failure_form() -> None:
    """Tier 1: mirrors #3329's own gutter-demotion exclusion — a FAILED
    retrieval call still needs the operator's attention regardless of the
    tool's op-class, so it is excluded from the one-line collapse."""
    msg = _settled_tool("no such file", "read_file", failed=True)
    body, _ = _body_and_background(msg, now=None, image_cache=None, decoded_image_cache=None)

    text = _plain(body)
    assert "no such file" in text
    assert "✗" in text
    assert "⎿" in text


def test_an_unsettled_retrieval_call_is_unaffected() -> None:
    """Tier 1: accept-side — a RUNNING (not yet settled) retrieval call has
    no result to fold; #4662's collapse never fires before a result lands."""
    msg = OutboxMessage(
        kind="tool_call_started", text="read_file",
        meta={"tool": "read_file", "args": {"path": "a.py"}},
    )
    body, _ = _body_and_background(msg, now=None, image_cache=None, decoded_image_cache=None)

    text = _plain(body)
    assert "→" not in text
    assert "read_file" in text
