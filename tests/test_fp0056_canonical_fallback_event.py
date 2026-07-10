"""Tier 1/2: the ``canonical_fallback_used`` audit event (FP-0056 PR-F2).

PR-F1 made canonical coverage a *static* CI gate; PR-F2 makes the runtime fallback *visible* instead of
silent — the observability half of the same contract. When a tool/op result takes the whole-dict
canonical fallback (a #2681 ``CANONICAL_TODO`` producer, a genuinely-unregistered source, or a
``STRUCTURED_PASSTHROUGH`` producer whose whole-dict blob overflows the offload gate), the live
``to_canonical`` callers emit a P6 audit event naming the source. Degrade-with-audit, never silently:
the 2026-07-09 dogfood incident (a doc read offloaded as a whole-dict blob) would have been one
trace-grep instead of a human noticing the agent "being confused".

The event is an AUDIT / P6 event — NOT a WAL / recovery-core event (no truncate-falsify obligation).

Covered here with real instances (no mocks — a real ``PipelineExecutor``, a real ``EventLog``, the real
registration seams):

- Tier 1: :func:`canonical_fallback_reason` maps each of the three fire-conditions to its reason
  category, and returns ``None`` for a real mapper and for a small (inline) passthrough.
- Tier 2: the pipeline tool-step chokepoint EMITS ``canonical_fallback_used`` carrying the source id on
  the fallback path, and NO result content bytes appear in any event payload.
- Tier 2 falsify: a producer WITH a real mapper (``sandboxed_exec``) does NOT emit the event.
"""
from __future__ import annotations

import json

import pytest

# Importing the op-runtime package eagerly registers every op handler + its canonical declaration, and
# building the default tool registry declares every ToolDefinition's — so the declarations the
# classifier resolves against are populated (identically to the coverage-gate test's setup).
import reyn.core.op_runtime as _op_runtime  # noqa: F401
from reyn.core.events.events import EventLog
from reyn.core.offload.canonical import (
    CANONICAL_FALLBACK_EVENT,
    CANONICAL_TODO,
    canonical_fallback_reason,
    declare_canonical,
)
from reyn.core.pipeline.executor import Pipeline, PipelineExecutor, ToolStep
from reyn.tools import get_default_registry

get_default_registry()


# A ``CANONICAL_TODO``-declared producer used only to confirm the classifier reports the
# ``canonical_todo`` reason for a declared-debt producer. After the #2681 burn-down (Buckets A/B/C)
# the live ``CANONICAL_TODO`` set is EMPTY — no REAL registered producer carries the marker anymore
# (the ratchet gate in ``test_fp0056_canonical_coverage_gate.py`` now enforces an empty ledger), so
# there is no ledger member to point at. The ``canonical_todo`` classifier branch is still live code
# (a future producer could be re-added to the ledger via a review-gated edit), so this exercises it
# via a SYNTHETIC fixture source declared ``CANONICAL_TODO``. ``declare_canonical`` is idempotent for
# the same sentinel + id, so repeated runs / imports don't conflict; the fixture id is neither an op
# kind nor a ToolDefinition, so it never enters the ratchet gate's registry-derived source set.
_A_CANONICAL_TODO_PRODUCER = "fixture_canonical_todo_fallback_source"
declare_canonical(_A_CANONICAL_TODO_PRODUCER, CANONICAL_TODO)
# An admin/install producer declared STRUCTURED_PASSTHROUGH (owner decision #1 family).
_A_PASSTHROUGH_PRODUCER = "mcp_install"
# A producer with a REAL mapper — the falsify control.
_A_MAPPED_PRODUCER = "sandboxed_exec"


def test_fallback_reason_maps_each_fire_condition_to_its_category() -> None:
    """Tier 1: the three fail-visible fire-conditions each yield their reason category, and a real
    mapper / a small passthrough yield ``None`` (no event) — the classifier the two chokepoints share."""
    # 1. genuinely unregistered / unknown source (+ None) → the lossless whole-dict fallback.
    assert canonical_fallback_reason("a_totally_unregistered_producer") == "unregistered"
    assert canonical_fallback_reason(None) == "unregistered"
    # 2. a declared-but-unmapped CANONICAL_TODO producer (the #2681 debt) → visible.
    assert canonical_fallback_reason(_A_CANONICAL_TODO_PRODUCER) == "canonical_todo"
    # 3. a STRUCTURED_PASSTHROUGH producer whose whole-dict blob overflowed the offload gate.
    assert (
        canonical_fallback_reason(_A_PASSTHROUGH_PRODUCER, structured_offloaded=True)
        == "passthrough_oversized"
    )
    # A SMALL (inline) passthrough is the reviewed, legitimate whole-dict view → no event.
    assert canonical_fallback_reason(_A_PASSTHROUGH_PRODUCER, structured_offloaded=False) is None
    # A producer with a real mapper never took a fallback → no event (even if oversized somewhere).
    assert canonical_fallback_reason(_A_MAPPED_PRODUCER) is None
    assert canonical_fallback_reason(_A_MAPPED_PRODUCER, structured_offloaded=True) is None


@pytest.mark.asyncio
async def test_unregistered_fallback_emits_event_with_source_id_and_no_body() -> None:
    """Tier 2: a pipeline tool step whose result took the whole-dict fallback (an unregistered source)
    emits ``canonical_fallback_used`` naming the source — and NO result content bytes leak into any
    event payload (audit signal, not data)."""
    secret_body = "SECRET-DOCUMENT-BODY-should-never-reach-an-audit-event"

    def _dispatch(name: str, args: dict) -> dict:
        # A kind-bearing but UNREGISTERED producer, tagged with its invoked identity — the genuine-
        # unknown fallback class. The body carries a distinctive string we assert never leaks.
        return {
            "kind": "mystery_kind",
            "content": secret_body,
            "_canonical_source": "an_unregistered_producer_id",
        }

    events = EventLog()
    pipeline = Pipeline(steps=[ToolStep(name="mystery_tool", args={}, output="r")])
    await PipelineExecutor().run(
        pipeline, None,
        tool_dispatch=_dispatch, state_log=None, run_id="run-fp0056-f2-fallback", events=events,
    )

    # Exactly one fallback event for the one unregistered tool step (unpack asserts the count).
    [fallback_event] = [e for e in events.all() if e.type == CANONICAL_FALLBACK_EVENT]
    payload = fallback_event.data
    assert payload["source"] == "an_unregistered_producer_id"
    assert payload["reason"] == "unregistered"

    # No result content bytes in ANY event payload — the event carries the source identity only.
    all_payloads = json.dumps([e.data for e in events.all()])
    assert secret_body not in all_payloads
    assert "content" not in payload  # the raw result field name is not forwarded either


@pytest.mark.asyncio
async def test_real_mapper_producer_does_not_emit_fallback_event() -> None:
    """Tier 2: FALSIFY — a producer WITH a real canonical mapper (``sandboxed_exec``) is shaped cleanly —
    it never took a fallback, so NO ``canonical_fallback_used`` event fires."""

    def _dispatch(name: str, args: dict) -> dict:
        return {
            "kind": "sandboxed_exec",
            "stdout": "clean output",
            "returncode": 0,
            "_canonical_source": _A_MAPPED_PRODUCER,
        }

    events = EventLog()
    pipeline = Pipeline(steps=[ToolStep(name="run_shell", args={}, output="r")])
    await PipelineExecutor().run(
        pipeline, None,
        tool_dispatch=_dispatch, state_log=None, run_id="run-fp0056-f2-mapped", events=events,
    )

    assert not [e for e in events.all() if e.type == CANONICAL_FALLBACK_EVENT], (
        "a mapped producer must not emit the fallback audit event"
    )
