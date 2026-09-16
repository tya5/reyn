"""Tier 1/2: #6184 段3-2 — the producer resolves ToolDefinition.subject_params
(段3-1) into OutboxMessage.subject, starting with exec's own first
declaration.

Ruling: lead-coder, issuecomment-5685519787. Display is UNCHANGED (accept
③ — the consumer does not read `subject` yet; landing this is itself the
stage's own accept criterion, matching #6184's own established additive
pattern for every stage before this one that did not itself wire a
consumer).

New layer edge (dispatch requirement ③, verbatim): ``lifecycle_forwarder.py``
gets its first import of ``reyn.tools`` — a genuinely NEW edge for THIS
module (it already refuses a ``gutter.py`` import for the same
layer-inversion reason, ``_compact_token_count``'s own docstring), but NOT
a new edge for the codebase as a whole (``runtime -> tools`` already
exists: ``registry.py:5988``, ``router_loop.py:43,61,1254``).

Real ``ToolDefinition``/``ChatLifecycleForwarder``/the real
``resolve_tool_subject``/``get_default_registry`` throughout (CLAUDE.md
mock ban) — no curated tool-name list; the "78 undeclared tools stay
None" witness reads the REAL default registry's own tool count, not a
hand-typed number.

#6184 BLOCKING (lead-coder, measured, PR #6200 review): a first version
of this stage had ``resolve_tool_subject`` return ``str(args[param])``
directly — for ``exec``'s own ``argv`` (a ``list[str]``), that produced
a Python repr (``"['python', '-m', 'pytest']"``), the EXACT display
shape the owner's original request asked to move away from. Fixed per
the architect's own 段3 design (issuecomment-5685059570):
``resolve_tool_subject`` now returns the RAW value, and
``core/present/tool_call_compose.render_subject`` converts it (str →
as-is, ``list[str]`` → space-joined, else → the existing normalize
path). ``test_exec_argv_list_becomes_a_space_joined_command_line``
below is the accept criterion lead-coder's own review required,
verbatim: "`exec` の `argv=['python','-m','pytest']` が
`python -m pytest` になる — `str()` に戻したら赤".
"""
from __future__ import annotations

import asyncio

from reyn.core.present.tool_call_compose import render_subject
from reyn.runtime.lifecycle_forwarder import ChatLifecycleForwarder
from reyn.tools import get_default_registry
from reyn.tools.subject import resolve_tool_subject


def test_declared_and_undeclared_tools_in_one_test():
    """Tier 1: dispatch's own accept ⑴⑵, DELIBERATELY combined in one test
    (dispatch, verbatim: "同じ test に" — asserting the None side alone
    stays green even with the whole mechanism deleted; this test bites
    on either half breaking).

    - A tool with no subject_params declaration (every one of the 78
      other real, currently-registered tools) resolves to None.
    - exec (the one declared tool, 段3-2's own first declaration)
      resolves to its `cmd` value when present.
    """
    registry = get_default_registry()
    undeclared_tool = next(
        t.name for t in registry if t.name != "exec" and not t.subject_params
    )
    assert resolve_tool_subject(undeclared_tool, {"some_param": "value"}) is None
    assert resolve_tool_subject("exec", {"cmd": "ls -la"}) == "ls -la"


def test_exec_falls_back_to_argv_when_cmd_absent():
    """Tier 1: exec's own declared priority order (`cmd` first, `argv`
    second) — a call that only supplies `argv` still resolves a RAW
    subject value (the list itself — resolve_tool_subject does not
    convert it, see render_subject below for the conversion), not
    None."""
    assert resolve_tool_subject("exec", {"argv": ["ls", "-la"]}) == ["ls", "-la"]
    assert resolve_tool_subject("exec", {}) is None


def test_exec_argv_list_becomes_a_space_joined_command_line():
    """Tier 2: #6184 BLOCKING accept criterion (lead-coder, verbatim,
    PR #6200 review) — the END-TO-END producer pipeline
    (resolve_tool_subject -> render_subject -> OutboxMessage.subject)
    turns exec's own argv list into a real command line, never a
    Python repr. Goes RED if either function regresses to str()."""
    raw = resolve_tool_subject("exec", {"argv": ["python", "-m", "pytest"]})
    assert render_subject(raw) == "python -m pytest"

    outbox: asyncio.Queue = asyncio.Queue()
    fwd = ChatLifecycleForwarder(outbox=outbox)
    fwd.on_tool_called({
        "caller_kind": "router", "caller_id": "r1", "tool": "exec",
        "chain_id": "c1", "args": {"argv": ["python", "-m", "pytest"]},
        "args_hash": "h1",
    })
    msg = outbox.get_nowait()
    assert msg.subject == "python -m pytest"


def test_exec_still_has_a_subject_params_declaration():
    """Tier 1: accept ⑷ AT 段3-2's OWN MERGE TIME this asserted "exactly
    one tool (exec) declares subject_params" — true then, made false ON
    PURPOSE by #6184 段3-4 (a LATER stage, same arc, issuecomment-
    5690351417: every OTHER tool whose own param is "identifying from
    the first character" also gets one) — the SAME "merge-time
    observation, not a standing invariant" shape #6190/#6191's own tests
    already disclosed. Re-scoped rather than re-pinning a new exact
    count (which 段3-4 itself would immediately outdate again the next
    time a tool is added): exec's OWN declaration surviving is what this
    file is actually about; the full current population lives in
    ``tests/tools/test_6184_stage3_4_other_tools_subject_params.py``."""
    registry = get_default_registry()
    declared = {t.name for t in registry if t.subject_params}
    assert "exec" in declared


def test_enqueue_tool_call_wires_subject_for_exec():
    """Tier 2: the real producer call site — ChatLifecycleForwarder.
    _enqueue_tool_call (via on_tool_called) sets OutboxMessage.subject
    from the real resolver, not a second, independent computation."""
    outbox: asyncio.Queue = asyncio.Queue()
    fwd = ChatLifecycleForwarder(outbox=outbox)
    fwd.on_tool_called({
        "caller_kind": "router", "caller_id": "r1", "tool": "exec",
        "chain_id": "c1", "args": {"cmd": "echo hi"}, "args_hash": "h1",
    })
    msg = outbox.get_nowait()
    assert msg.subject == "echo hi"


def test_enqueue_tool_call_leaves_subject_none_for_an_undeclared_tool():
    """Tier 2: sibling of the test above — a real tool with no
    subject_params declaration produces subject=None through the SAME
    real producer call site, not a special-cased None.

    ``list_tasks`` (not ``read_file``, this test's own original choice):
    #6184 段3-4 (a LATER stage, same arc) declared ``read_file``'s own
    ``path`` a subject — it stopped being an "undeclared tool" example.
    ``list_tasks`` is lead-coder's own canonical deny-side example
    throughout that stage's dispatch (its ``kind`` param is a FILTER on
    one global listing, never an identity) — asserted still-undeclared
    below rather than assumed, so a THIRD stage declaring it would fail
    this test's own setup, not silently pass a stale assertion."""
    registry = get_default_registry()
    definition = registry.lookup("list_tasks")
    assert definition is not None and not definition.subject_params, (
        "setup: list_tasks must still be undeclared for this test's own "
        "premise to hold"
    )

    outbox: asyncio.Queue = asyncio.Queue()
    fwd = ChatLifecycleForwarder(outbox=outbox)
    fwd.on_tool_called({
        "caller_kind": "router", "caller_id": "r1", "tool": "list_tasks",
        "chain_id": "c1", "args": {"kind": "cron"}, "args_hash": "h2",
    })
    msg = outbox.get_nowait()
    assert msg.subject is None
