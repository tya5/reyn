"""Tier 2: act_executed carries op_kinds + forwarder surfaces them (#161 #3).

Pre-fix the ``act_executed`` event payload only carried ``op_count``,
so the per-skill forwarder emitted a generic ``⤷ act: 2 ops`` detail
line with no signal about which op kinds ran. The user could not
distinguish "parent ran a file-write batch" from "parent spawned a
sub-skill" — both looked identical on the parent's SkillActivityRow.

Fix (= direction (d) from the #161 owner decision):
  1. ``PhaseExecutor`` includes ``op_kinds: list[str]`` in the
     ``act_executed`` payload (additive — no schema bump for
     subscribers that read other fields).
  2. ``ChatEventForwarder.on_act_executed`` reads ``op_kinds`` and
     appends a parenthetical "(run_skill, write_file)" to the detail
     text. De-duplicates while preserving first-occurrence order,
     truncates to first 3 with ``…`` so the line stays short.

This test file pins the forwarder side — the kernel-side emit shape
is exercised through end-to-end tests; here we only verify that the
forwarder consumes the new field correctly and that the legacy
``op_kinds``-absent payload still works.
"""
from __future__ import annotations

import asyncio
from typing import Any

from reyn.runtime.forwarder import ChatEventForwarder
from reyn.schemas.models import Event


def _drain(q: asyncio.Queue) -> list[Any]:
    items: list[Any] = []
    while not q.empty():
        items.append(q.get_nowait())
    return items


def test_act_executed_with_op_kinds_surfaces_distinct_kinds() -> None:
    """Tier 2: op_kinds list → "(run_skill, write_file)" suffix on detail."""
    q: asyncio.Queue = asyncio.Queue()
    fwd = ChatEventForwarder("router", q, run_id="r-1")
    fwd(Event(
        type="act_executed",
        data={
            "op_count": 2,
            "op_kinds": ["run_skill", "write_file"],
            "run_id": "r-1",
        },
    ))
    msgs = _drain(q)
    (only,) = msgs
    assert "act: 2 ops" in only.text
    assert "(run_skill, write_file)" in only.text


def test_act_executed_deduplicates_repeated_kinds() -> None:
    """Tier 2: repeated kinds collapse to first occurrence ("a, b" not "a, a, b")."""
    q: asyncio.Queue = asyncio.Queue()
    fwd = ChatEventForwarder("s", q, run_id="r-1")
    fwd(Event(
        type="act_executed",
        data={
            "op_count": 3,
            "op_kinds": ["run_skill", "run_skill", "write_file"],
        },
    ))
    msgs = _drain(q)
    # "run_skill, write_file" — not "run_skill, run_skill, write_file"
    text = msgs[0].text
    assert text.count("run_skill") == 1
    assert "write_file" in text


def test_act_executed_truncates_to_three_kinds_with_ellipsis() -> None:
    """Tier 2: more than 3 distinct kinds → first 3 + "…" tail.

    Keeps the detail line short on heavy batches; the full op list is
    still in the event payload for the Events tab.
    """
    q: asyncio.Queue = asyncio.Queue()
    fwd = ChatEventForwarder("s", q, run_id="r-1")
    fwd(Event(
        type="act_executed",
        data={
            "op_count": 5,
            "op_kinds": ["a", "b", "c", "d", "e"],
        },
    ))
    msgs = _drain(q)
    text = msgs[0].text
    assert "(a, b, c…)" in text
    # "d" and "e" must NOT appear inside the parenthetical (the bare
    # word "detail" naturally contains 'd', so we constrain the check
    # to the parens substring).
    parens = text[text.index("(") : text.index(")") + 1]
    assert "d" not in parens
    assert "e" not in parens


def test_act_executed_without_op_kinds_unchanged() -> None:
    """Tier 2: pre-fix payload (no op_kinds key) still emits a usable detail.

    Backward-compat: events generated before this PR (= replay fixtures,
    pre-rollout sessions resumed mid-flight) carry no op_kinds. Forwarder
    must still emit "act: N ops" without the parenthetical.
    """
    q: asyncio.Queue = asyncio.Queue()
    fwd = ChatEventForwarder("s", q, run_id="r-1")
    fwd(Event(type="act_executed", data={"op_count": 2}))
    msgs = _drain(q)
    assert msgs[0].text == "detail: act: 2 ops"


def test_act_executed_empty_op_kinds_falls_back_to_count_only() -> None:
    """Tier 2: empty list behaves like missing field (= count-only output).

    Defensive against producers that might emit op_kinds=[] for zero-op
    batches; we still want a clean detail line.
    """
    q: asyncio.Queue = asyncio.Queue()
    fwd = ChatEventForwarder("s", q, run_id="r-1")
    fwd(Event(
        type="act_executed",
        data={"op_count": 1, "op_kinds": []},
    ))
    msgs = _drain(q)
    assert msgs[0].text == "detail: act: 1 op"
