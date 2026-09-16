"""Tier 1: #6184 段3-4 — `exec` was the ONLY tool with a declared
`subject_params` since 段3-2; this stage applies the discriminator
lead-coder ruled (issuecomment-5690351417, refining architect's own
issuecomment-5689016062) to every other `ToolDefinition` in
`src/reyn/tools/`:

    ① is there a value that should let a reader tell two calls of the
       SAME tool apart?
    ② is that value identifying from its own FIRST characters (subject
       is prefixed and never truncated from the front — a value whose
       distinguishing content sits after a boilerplate opening, e.g.
       `ask_user`'s `question` or `compact`'s `reason`, does not pass
       this even when ① holds)?

Full 78-tool (exec excluded) classification table, with a 1-line reason
per tool, lives on the PR body and the issue thread (#6184#issuecomment-
5690342009, ruled #6184#issuecomment-5690351417) — not repeated here;
this file pins the CONTRACT the classification produces (a declared
tool's subject reaches the screen at the line's head; an undeclared
tool's does not), not the classification itself.

Two representative YES tools (chosen to also exercise the priority-
tuple shape, not just a single-param declaration):
  - `read_file` — single param (`path`).
  - `hooks_add` — priority tuple (`name`, `on`) — `on` wins when `name`
    is absent, the SAME shape `exec`'s own `(cmd, argv)` established.

One representative NO tool — `list_tasks`, lead-coder's own example
throughout this stage's dispatch (`kind` is a FILTER on one global
listing, not an identity) — the deny-side sibling: without it, "declare
a subject for every tool" would pass this file's YES-side tests just as
well as the real, discriminated declaration set does.
"""
from __future__ import annotations

import io

from rich.console import Console

from reyn.interfaces.inline.textual_chat.presenter import _tool_head
from reyn.interfaces.repl.renderer import format_inline_message
from reyn.runtime.outbox import OutboxMessage
from reyn.tools import get_default_registry
from reyn.tools.subject import resolve_tool_subject


def _plain_inline(msg: OutboxMessage) -> str:
    console = Console(width=200, file=io.StringIO(), color_system=None)
    console.print(format_inline_message(msg))
    return console.file.getvalue()


def _msg_for(tool_name: str, args: dict) -> OutboxMessage:
    """Builds a real `OutboxMessage` the SAME way `lifecycle_forwarder.
    _enqueue_tool_call` does — `resolve_tool_subject` against the REAL
    registry, not a hand-typed `subject=` — so a stale declaration (a
    renamed param the gate would already catch) is not masked here by a
    synthetic value that no longer matches the real tool."""
    raw = resolve_tool_subject(tool_name, args)
    subject = None
    if raw is not None:
        from reyn.core.present.tool_call_compose import render_subject
        subject = render_subject(raw)
    return OutboxMessage(
        kind="tool_call_started", text=tool_name,
        meta={"tool": tool_name, "args": args}, subject=subject,
    )


def test_read_file_subject_is_at_the_heads_of_both_render_paths() -> None:
    """Tier 1: `read_file`'s declared `path` reaches the screen, at the
    HEAD of the line, through both consumer sites (#6184 段3-3's own
    single compose point) — the real registry declaration this stage
    adds, not a synthetic one."""
    msg = _msg_for("read_file", {"path": "docs/x.md"})
    assert msg.subject == "docs/x.md"

    tool_head_out = _tool_head(msg).plain
    assert tool_head_out.startswith("read_file docs/x.md")

    inline_out = _plain_inline(msg)
    assert "read_file docs/x.md" in inline_out
    assert inline_out.index("docs/x.md") < inline_out.index("(")


def test_hooks_add_priority_tuple_on_wins_when_name_absent() -> None:
    """Tier 1: `hooks_add`'s declared `(name, on)` priority — the SAME
    "first present wins" shape `exec`'s own `(cmd, argv)` established
    (段3-2). When `name` is absent, `on` becomes the subject and reaches
    the head of the line."""
    msg = _msg_for("hooks_add", {"on": "tool_call_started", "message": "hi"})
    assert msg.subject == "tool_call_started"
    out = _tool_head(msg).plain
    assert out.startswith("hooks_add tool_call_started")


def test_hooks_add_priority_tuple_name_wins_when_present() -> None:
    """Tier 1: the accept-side of the priority test above — `name`, when
    given, wins over `on`."""
    msg = _msg_for("hooks_add", {"name": "my-hook", "on": "tool_call_started", "message": "hi"})
    assert msg.subject == "my-hook"


def test_list_tasks_has_no_declared_subject_deny_side() -> None:
    """Tier 1: DENY side — `list_tasks` (lead-coder's own canonical
    example: `kind` is a filter on one global listing, not an identity)
    declares NO `subject_params`. Without this test, "declare a subject
    for every tool" would pass every YES-side test above just as well as
    the real, discriminated 63/15 split does — this is what makes the
    split itself the thing under test, not merely "some tools work"."""
    definition = get_default_registry().lookup("list_tasks")
    assert definition is not None
    assert definition.subject_params == ()

    msg = _msg_for("list_tasks", {"kind": "cron"})
    assert msg.subject is None

    out = _tool_head(msg).plain
    assert not out.startswith("list_tasks cron")
