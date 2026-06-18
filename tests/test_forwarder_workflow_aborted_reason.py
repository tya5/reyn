"""Tier 2: ChatEventForwarder.on_workflow_aborted forwards reason in text.

C-F2 (wave-8 follow-up): before this fix, ``on_workflow_aborted``
emitted only ``"skill done: aborted"`` with no reason field. The
TUI's ``SkillActivityRow`` ``✗`` finish line rendered as
``"failed: N phase(s)"`` (phase visit count) regardless of the
actual abort cause — users had to switch to the events tab to see
the real reason (= ``budget_exceeded`` / ``timeout`` / etc).

Contract pinned here:

1. ``data["reason"]`` present + non-empty → emit
   ``"skill done: aborted: <reason>"``
2. ``data["reason"]`` absent / empty → emit bare
   ``"skill done: aborted"`` (= backward-compat for older event
   shapes that don't carry a reason field)
3. ``run_id`` propagation unchanged
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

_SRC = Path(__file__).parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from reyn.runtime.forwarder import ChatEventForwarder  # noqa: E402
from reyn.schemas.models import Event  # noqa: E402


def _drain(q: asyncio.Queue) -> list:
    out = []
    while not q.empty():
        out.append(q.get_nowait())
    return out


def test_on_workflow_aborted_with_reason_encodes_in_text() -> None:
    """Tier 2: reason field → ``"skill done: aborted: <reason>"``."""
    q: asyncio.Queue = asyncio.Queue()
    fwd = ChatEventForwarder("test_skill", q, run_id="parent-run")
    fwd(Event(
        type="workflow_aborted",
        data={"run_id": "child-run", "reason": "budget_exceeded"},
    ))
    msgs = _drain(q)
    (only,) = msgs
    assert only.text == "skill done: aborted: budget_exceeded"


def test_on_workflow_aborted_without_reason_emits_bare_text() -> None:
    """Tier 2: no reason field → bare ``"skill done: aborted"`` (back-compat)."""
    q: asyncio.Queue = asyncio.Queue()
    fwd = ChatEventForwarder("test_skill", q, run_id="parent-run")
    fwd(Event(type="workflow_aborted", data={"run_id": "child-run"}))
    msgs = _drain(q)
    assert msgs[0].text == "skill done: aborted"


def test_on_workflow_aborted_with_empty_reason_emits_bare_text() -> None:
    """Tier 2: empty-string reason → bare form (= treat as absent)."""
    q: asyncio.Queue = asyncio.Queue()
    fwd = ChatEventForwarder("test_skill", q, run_id="parent-run")
    fwd(Event(
        type="workflow_aborted",
        data={"run_id": "child-run", "reason": ""},
    ))
    msgs = _drain(q)
    assert msgs[0].text == "skill done: aborted"


def test_on_workflow_aborted_with_whitespace_only_reason_emits_bare_text() -> None:
    """Tier 2: whitespace-only reason → bare form (= treat as absent)."""
    q: asyncio.Queue = asyncio.Queue()
    fwd = ChatEventForwarder("test_skill", q, run_id="parent-run")
    fwd(Event(
        type="workflow_aborted",
        data={"run_id": "child-run", "reason": "   "},
    ))
    msgs = _drain(q)
    assert msgs[0].text == "skill done: aborted"


def test_on_workflow_aborted_run_id_provenance_preserved() -> None:
    """Tier 2: run_id propagation unchanged by the reason-encoding change."""
    q: asyncio.Queue = asyncio.Queue()
    fwd = ChatEventForwarder("test_skill", q, run_id="parent-run")
    fwd(Event(
        type="workflow_aborted",
        data={"run_id": "child-run", "reason": "timeout"},
    ))
    msg = _drain(q)[0]
    assert msg.meta["run_id"] == "child-run"
    assert msg.meta["parent_run_id"] == "parent-run"
