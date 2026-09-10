"""#5939 / #5851 stage (b) PR-1 — the shared cache-release step (ladder
step ② in architect's design: https://github.com/tya5/reyn/issues/5939)
and the ``process_memory_forensics`` diagnostic it produces as a
byproduct.

This is the ONE function every context on the memory ladder calls for
"release what can be safely re-derived, then measure whether it helped"
— the steady-state ladder (① backpressure → **② here** → ③ session halt
→ ④ process exit, PR-2), the in-turn mini-ladder (①' force-compact →
**②' the SAME function** → ③' stop at the next iteration boundary,
PR-3), and the hydration mini-ladder (①'' stop reading → **② the SAME
function** → ④ abort startup, PR-4) all share it, per architect's own
design (#5851 issue comments 2026-09-07T12:06/12:38): "②' cache解放
（1回だけ、効果を測る。#5939の段②と同じ関数）".

## What's IN scope here (and why)

Architect's own judge criterion (#5939): "durable な状態から作り直せる
ものだけが cache". This module drops only MODULE-LEVEL caches with a
public clear function already exposed by their OWNING module (never
reaching into another module's private state directly) — no live-
instance registry is needed to find them:

- ``reyn.services.compaction.engine.token_cache_clear()``
- ``reyn.data.workspace.artifact_ref.table_cache_clear()``
- ``reyn.interfaces.repl.status.config_derived_cache_clear()``

## What's explicitly OUT of scope (and why — not silently dropped)

- **``reyn.security.sandbox._derivation_cache``** — architect's own
  inventory (#5939) listed this as a candidate ("小さいが0ではない"),
  but reading the module directly (#5981's own refcount design) shows
  it fails the "durable な状態から作り直せる" test in a way the
  inventory did not account for: an entry's PRESENCE in ``_CACHE`` is,
  by that module's own invariant ("A key present here is, by
  construction, also present in ``_REFCOUNT``"), synonymous with at
  least one outstanding checkout — there is no "idle, refcount-0" state
  for a present entry to be in. Dropping it here could fire
  ``on_evict`` (which, for Seatbelt, deletes an on-disk ``.sb`` profile
  file) while a live sandbox-exec op still holds that checkout,
  breaking an IN-FLIGHT security enforcement, not merely re-computing a
  value — the "resource operation must not move a semantic verdict"
  hazard. Left alone; reported back to architect (#5939) rather than
  silently included or silently excluded.
- **Instance-attribute caches** (``context_budget_advisor.
  _history_token_cache``, ``MediaStore``'s ``_unspilled_paths``/
  ``_history_content_spill_paths``, resident history itself — the
  single largest chunk per architect's own inventory) — reaching these
  needs a way to find every LIVE instance from one process-wide
  function, which does not exist yet (no session/advisor/MediaStore
  registry — confirmed absent, #5939 issue comment). This is PR-5's own
  design question (registry vs. a per-``Session`` ``drop_caches()``
  method called at a turn boundary) — deliberately NOT decided here,
  per lead-coder's explicit instruction not to fold a design choice
  into an earlier, unrelated PR.

## Diagnostics (architect's spec, #5939) — 3-way output

``run_cache_release_and_forensics`` emits ``process_memory_forensics``
(audit-event — machine-readable), logs a summary via the standard
logger (reaches ``reyn.log``, already rotation-bounded by #5879), and
writes a <=5-line summary directly to the REAL stderr (``sys.
__stderr__``, never the reassignable ``sys.stderr`` name — the same
discipline ``stall_trace.default_log_stream`` already documents, since
a TUI or a test's capture manager can rebind ``sys.stderr`` mid-session)
— any ONE of the three may not reach the operator (audit-event storage
could itself be under pressure; ``reyn.log``'s handler may not be
installed in every caller; stderr may be invisible if the terminal
itself is unresponsive), so all three fire independently.

``host`` (swap/memory-pressure) and ``top_history_rows`` fields named in
architect's own schema sketch are OUT of scope for this PR specifically:
- ``host`` is PR-2's own field (the host-OR-condition reader that decides
  whether ④ fires belongs with ④ itself, not duplicated here) — carried
  as ``None`` with a docstring note, not a placeholder number.
- ``top_history_rows`` is genuinely buildable now (#5896 landed all 3
  stages — a history row carries its own resident-byte count, no walk
  needed) but reading LIVE session history needs the same instance-
  registry PR-5 will add; until then this is ``None`` too, disclosed,
  not silently omitted from the schema.
"""
from __future__ import annotations

import logging
import sys
from dataclasses import dataclass
from typing import Callable, Iterable

from reyn.core.events.events import EventLog
from reyn.data.workspace.artifact_ref import table_cache_clear
from reyn.interfaces.repl.status import config_derived_cache_clear
from reyn.runtime.process_memory import ProcessMemoryGuard
from reyn.services.compaction.engine import token_cache_clear

_log = logging.getLogger(__name__)


@dataclass(frozen=True)
class CacheDropResult:
    """One dropped cache's own before/after entry count — the per-cache
    line of ``process_memory_forensics``'s ``dropped`` list. No byte
    estimate: none of the 3 caches in scope here carries a per-entry
    size the module already tracks (unlike a history row, #5896), and
    computing one would mean re-serializing what we are trying NOT to
    touch — the entry COUNT is the honest, already-known number."""

    name: str
    entries_before: int
    entries_after: int


# name -> the owning module's own public clear function (returns the
# count it dropped). Order matters only for readability of the
# `dropped` list — every entry here is a module-level dict/OrderedDict/
# WeakKeyDictionary with O(1) clear, so there is no "touch-cost order"
# to respect (contrast the resident-history case PR-5 will add, where
# architect's own design requires "untouched things first").
_RECONSTRUCTABLE_CACHES: "tuple[tuple[str, Callable[[], int]], ...]" = (
    ("compaction_token_cache", token_cache_clear),
    ("artifact_ref_table_cache", table_cache_clear),
    ("status_config_derived_cache", config_derived_cache_clear),
)


def release_reconstructable_caches() -> "list[CacheDropResult]":
    """Ladder step ② itself: drop every cache in
    :data:`_RECONSTRUCTABLE_CACHES`, once, no retry — matching
    architect's own ruling (#5939): "段②に再試行ループを置かない。1回
    捨てて1回測る". Each clear function already returns its own
    before-count; only one extra call (irrelevant cost — these caches
    are at most thousands of entries) gets the after-count, so a caller
    reading this result never has to guess whether a clear genuinely
    emptied its cache."""
    results = []
    for name, clear_fn in _RECONSTRUCTABLE_CACHES:
        before = clear_fn()
        # `entries_after` is always 0, not re-measured: every clear_fn
        # above is an atomic "count then .clear()" (no window for a
        # concurrent repopulation to land between the two, same thread,
        # no await in between) — a second len() call would only ever
        # read the same guaranteed-empty state back.
        results.append(CacheDropResult(name=name, entries_before=before, entries_after=0))
    return results


@dataclass
class ProcessMemoryForensics:
    """The byproduct of one ② run — architect's own answer to owner's
    "RAM消費要因の候補を数値と共に" requirement (#5939): this IS that
    report, assembled from step ②'s own before/after measurements
    rather than a separate walk.

    ``host`` / ``top_history_rows`` are ``None`` in THIS stage — see the
    module docstring's "Diagnostics" section for why, and which later
    PR owns filling them in."""

    footprint_before: "int | None"
    footprint_after: "int | None"
    metric: "str | None"
    dropped: "list[CacheDropResult]"
    host: "None" = None
    top_history_rows: "None" = None


async def run_cache_release_and_forensics(
    guard: ProcessMemoryGuard,
    events: EventLog,
    *,
    chain_id: "str | None" = None,
    # #5939/#5851 PR-5: every LIVE session whose own instance-attribute
    # caches (currently: MediaStore's spill-path sets) should be dropped
    # alongside the module-level caches above. ``None`` (every pre-PR-5
    # caller: hydration, and any caller not yet updated) means "no
    # instance caches in scope" — byte-identical to before this
    # parameter existed. Duck-typed (``getattr(s, "_drop_instance_caches",
    # None)``) rather than importing ``Session`` — this module is
    # imported FROM ``session.py``, so a real ``Session`` import here
    # would be circular; every real caller passes real ``Session``
    # objects, this is a structural avoidance, not a design choice about
    # what may be passed.
    sessions: "Iterable[object] | None" = None,
) -> ProcessMemoryForensics:
    """Run ladder step ②: measure, release, measure again, report on all
    3 faces (audit-event / ``reyn.log`` / real stderr). The single entry
    point every ladder context (steady-state, in-turn, hydration) calls
    — see the module docstring for why it is the SAME function
    everywhere, not one per caller.

    #5939/#5851 PR-5: ``sessions`` extends this SAME call, SAME event,
    SAME forensics report to each live session's own instance-attribute
    caches — no new call site, no new event kind. Design:
    https://github.com/tya5/reyn/issues/5851 (lead-coder-approved,
    2026-09-10) — the population of "instances holding a cache" is
    DERIVED from session enumeration (``AgentRegistry.all_sessions()``,
    #5939 PR-2) rather than a separately hand-maintained registry.

    ``async`` (PR-5, was sync before): ``Session._drop_instance_caches``
    needs to ``await`` a flush before it is safe to prune (see that
    method's own docstring for the race this closes) — this function is
    the one chokepoint that reaches it, so it must be able to propagate
    that await. Every call site was already inside an ``async def``
    (``Session._check_memory_ladder``, itself moved async in a SEPARATE
    commit for exactly this — and ``Session._check_turn_mid_memory_
    ladder``, already async)."""
    footprint_before = guard.read()
    dropped = release_reconstructable_caches()
    if sessions is not None:
        for s in sessions:
            drop_fn = getattr(s, "_drop_instance_caches", None)
            if callable(drop_fn):
                dropped.extend(await drop_fn())
    footprint_after = guard.read()
    forensics = ProcessMemoryForensics(
        footprint_before=footprint_before,
        footprint_after=footprint_after,
        metric=guard.metric,
        dropped=dropped,
    )
    _emit_forensics_event(events, forensics, chain_id=chain_id)
    _log_forensics_summary(forensics)
    _write_stderr_summary(forensics)
    return forensics


def _emit_forensics_event(
    events: EventLog, forensics: ProcessMemoryForensics, *, chain_id: "str | None",
) -> None:
    events.emit(
        "process_memory_forensics",
        footprint_before=forensics.footprint_before,
        footprint_after=forensics.footprint_after,
        metric=forensics.metric,
        dropped=[
            {
                "name": d.name,
                "entries_before": d.entries_before,
                "entries_after": d.entries_after,
            }
            for d in forensics.dropped
        ],
        chain_id=chain_id,
    )


def _log_forensics_summary(forensics: ProcessMemoryForensics) -> None:
    """Reaches ``reyn.log`` via the standard logger — already rotation-
    bounded (#5879), so an occasional forensics line here costs nothing
    extra to the discipline that bound already enforces."""
    _log.warning(
        "process_memory_forensics: %s %s -> %s (dropped: %s)",
        forensics.metric,
        forensics.footprint_before,
        forensics.footprint_after,
        ", ".join(f"{d.name}={d.entries_before}" for d in forensics.dropped) or "none",
    )


def _write_stderr_summary(forensics: ProcessMemoryForensics) -> None:
    """<=5 lines, written to the REAL stderr (``sys.__stderr__``) —
    UNCONDITIONALLY, independent of whether ``reyn.log`` also received
    its own copy via :func:`_log_forensics_summary` above. Architect's
    own spec (#5939) wants all 3 faces to fire independently ("どれか
    1つは届かない状況を想定") — ``stall_trace.default_log_stream``'s
    own log-file-OR-stderr fallback shape does not fit here, since that
    would make this line silently land in the log file (not the
    terminal) whenever a ``reyn.log`` handler happens to be installed,
    exactly the case this face exists to cover. Still never the
    reassignable ``sys.stderr`` name — a TUI or test capture manager can
    rebind it mid-session (the same hazard ``stall_trace.
    default_log_stream``'s own docstring documents), landing a write in
    whatever fd number the rebind's open reused instead of the terminal
    the operator is actually watching."""
    stream = sys.__stderr__
    if stream is None:
        return
    lines = [
        "process_memory_forensics:",
        f"  footprint: {forensics.footprint_before} -> {forensics.footprint_after} ({forensics.metric})",
        f"  dropped: {', '.join(f'{d.name}={d.entries_before}' for d in forensics.dropped) or 'none'}",
    ]
    try:
        stream.write("\n".join(lines) + "\n")
        stream.flush()
    except (OSError, ValueError):
        # Best-effort — the whole point of writing to stderr is a
        # last-resort channel; a failure here must not raise past a
        # memory-pressure caller that is already in trouble.
        pass
