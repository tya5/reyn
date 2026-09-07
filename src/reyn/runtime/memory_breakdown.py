"""#5959 (owner-hit, severity:high) — "leak 容疑を process の外から判定できない":
owner, verbatim: "今後も leak 容疑確認のために使用量見積もりのプロファイル取れ
るようにすべきじゃないの" / "python object として reyn が参照してるってこと
なんでしょ？" A `vmmap` region-distribution read (#5957) cannot tell "an
allocator-held free region" from "a live Python reference holding it" — both
look identical from OUTSIDE the process. This module is the IN-PROCESS
answer: it walks the interpreter's own live object graph.

## Two populations, two different jobs — never one curated list (architect
## ruling, issuecomment on #5959: an earlier draft that hand-listed "reyn's
## own known big consumers" was exactly the class this repo's own
## discipline forbids — "count what exists, not what you already suspect")

- **ⓐ discovery** (:func:`gc_type_breakdown` / :func:`gc_largest_objects`):
  ``gc.get_objects()``, grouped by type. No curated list — nothing here can
  go stale, because it enumerates whatever is ACTUALLY alive, not a
  hand-maintained guess at what might be. Answers "who is holding this" by
  naming the TYPE, not by asking the operator to already suspect it.
- **ⓑ completeness** (:func:`bounding_axis_census`): every config field
  carrying ``metadata={"axis": Axis.BOUNDING}`` — derived from
  :func:`reyn.config.config_schema.walk_config_schema`, never a hand-
  written second list. A field this repo has already declared "should be
  bounded," paired with its declared cap and (if a measurement provider is
  registered) its current live value — ``unmeasured`` when none is, a
  DIFFERENT value from ``0`` (an operator must never read "we didn't check"
  as "it's empty").

The tool's real product is what a HUMAN does with both tables side by side:
a type large in ⓐ with nothing in ⓑ that looks like it corresponds is a
candidate "nobody declared this bounded" resource (ADR-0046's Falsification
section); a ⓑ field reading 0/``unmeasured`` is a candidate dead
declaration. This module does not attempt to MECHANICALLY join the two —
they live in different namespaces (a Python type name vs. a config dotted
key) with no reliable general mapping between them; :func:`bounding_axis_
census`'s own output is filterable to exactly the rows a reader needs for
that second judgment (measured in ``(0, None)``), which is the one
concrete, testable claim architect's own corrected acceptance text asks
for (issuecomment: "``Axis.BOUNDING`` の field で、実測値が 0 または
``unmeasured`` のものが表に出る").

## `sys.getsizeof` is SHALLOW — a container's own size, not its contents'
(a list of 1000 large strings reports its own pointer-array size, not the
strings). This module names itself an ESTIMATE everywhere it surfaces
(docstrings, CLI output, the audit-event's own ``note`` field) — the goal
is ranking SUSPECTS, not an exact byte count (architect: "厳密さより容疑者
の順位が目的").

## ⚠️ A DISCLOSED, SHARPER gap ⓐ cannot see AT ALL — not merely shallow,
INVISIBLE: `gc.get_objects()` only enumerates objects CPython's cyclic
garbage collector TRACKS (containers, class instances with `__dict__` —
anything that could participate in a reference cycle). `str` / `bytes` /
`bytearray` / `int` / `float` are NEVER tracked (`gc.is_tracked("x")` is
`False`) — so a large STRING held via any reference (a variable, a dict
value, `ChatMessage.content`) is invisible to :func:`gc_type_breakdown`/
:func:`gc_largest_objects` BOTH directly (the string itself never
appears as a row) AND indirectly (the shallowness above means the
REFERENCING container's row does not include it either). This is exactly
the shape owner's own suspicion names — a large content string held by a
live Python reference — so naming this gap here, not leaving it silent,
matters: ⓐ's live walk genuinely cannot answer that specific question;
:func:`tracemalloc_top_allocations` (stage ③, which attributes by
ALLOCATION SITE rather than a live object walk, and so sees every
allocation regardless of gc-tracking) is the tool that can. Verified
directly: ``tests/runtime/test_5959_memory_breakdown.py``'s own
``test_a_large_standalone_string_is_invisible_to_gc_based_discovery``.

## `tracemalloc` (opt-in, deep dive, stage ③) lives in this module too
(:func:`tracemalloc_is_armed` / :func:`arm_tracemalloc_if_requested` /
:func:`tracemalloc_top_allocations`) — gated on ``REYN_MEMORY_TRACE``
being set at PROCESS START (CLI ``main()``'s own entry, before any other
import does real work): tracemalloc's own per-allocation overhead only
buys anything if it was armed before the allocations under suspicion
happened, so arming it lazily on first use would already be too late for
the allocations that mattered. When unset, this module executes ZERO
tracemalloc calls — not "started then stopped," genuinely never invoked.
"""
from __future__ import annotations

import gc
import os
import sys
from dataclasses import dataclass
from typing import Callable

#: The env var that arms tracemalloc — read ONCE, at process start
#: (:func:`arm_tracemalloc_if_requested`, called from CLI ``main()``).
#: Not re-read later: a mid-process ``os.environ`` mutation must not
#: retroactively arm tracing for allocations that already happened.
TRACEMALLOC_ENV_VAR = "REYN_MEMORY_TRACE"

#: A measurement provider: given nothing, returns the CURRENT live byte
#: count for one BOUNDING-axis config field's real-world counterpart, or
#: ``None`` if it genuinely cannot measure right now (never fabricated).
MeasurementProvider = Callable[[], "int | None"]


@dataclass(frozen=True)
class TypeBreakdownRow:
    """One :func:`gc_type_breakdown` row — a TYPE's aggregate footprint
    estimate, never a single object's."""

    type_name: str
    count: int
    total_sizeof_bytes: int


@dataclass(frozen=True)
class LargestObjectRow:
    """One :func:`gc_largest_objects` row — a single object, not a type
    aggregate. ``repr_snippet`` is capped and best-effort (``repr()`` on an
    arbitrary live object can itself raise or be unbounded; see that
    function's own guard)."""

    type_name: str
    sizeof_bytes: int
    repr_snippet: str


@dataclass(frozen=True)
class BoundingCensusRow:
    """One :func:`bounding_axis_census` row. ``measured_bytes is None``
    means ``unmeasured`` — genuinely distinct from a real ``0`` (no
    provider is registered for this field, or the registered one could
    not measure right now), never conflated by this dataclass's own
    shape."""

    key: str
    declared_cap: "object"
    measured_bytes: "int | None"


@dataclass(frozen=True)
class MemoryBreakdown:
    """The full #5959 snapshot — ⓐ (both discovery tables) + ⓑ (the
    completeness census) + the dead-declaration filter of ⓑ, bundled for
    one audit-event emit / one CLI report so both surfaces always show the
    SAME numbers from the SAME ``gc.get_objects()`` pass (never re-walked
    between the two, which could silently diverge under concurrent
    allocation)."""

    type_breakdown: "list[TypeBreakdownRow]"
    largest_objects: "list[LargestObjectRow]"
    bounding_census: "list[BoundingCensusRow]"

    @property
    def unbounded_or_dead_declarations(self) -> "list[BoundingCensusRow]":
        """architect's corrected acceptance text, verbatim: every
        ``Axis.BOUNDING`` field whose measured value is 0 or
        ``unmeasured`` — candidates for #5959's "type A dead declaration"
        (a resource declared bounded with nothing measurably resident
        under it). Named ``unbounded_or_dead_declarations`` rather than
        ``dead_declarations`` alone: an ``unmeasured`` row is NOT yet
        known to be dead — only that nobody has wired a live reading for
        it — so this property's own name does not overclaim what an
        ``unmeasured`` row means (see the module docstring's own "type A
        vs type B" distinction — this module can only ever speak to A)."""
        return [
            row for row in self.bounding_census
            if row.measured_bytes is None or row.measured_bytes == 0
        ]


#: #5959: the size a container's own `sys.getsizeof` reports never
#: includes what a REPR of an arbitrary live object might do (a custom
#: `__repr__` can itself be arbitrarily expensive, or raise) -- capped
#: hard so one pathological object never dominates a scan's wall time.
_REPR_SNIPPET_MAX_CHARS = 200


def _safe_repr_snippet(obj: object, *, max_chars: int = _REPR_SNIPPET_MAX_CHARS) -> str:
    """``repr(obj)``, capped and never raising — a live object pulled
    from ``gc.get_objects()`` may belong to ANY type in the process,
    including one whose ``__repr__`` itself raises or recurses; this
    function's whole job is to make that irrelevant to the scan around
    it."""
    try:
        text = repr(obj)
    except Exception as exc:  # noqa: BLE001 - an arbitrary live object's __repr__ may raise anything
        return f"<repr() raised {type(exc).__name__}>"
    if len(text) > max_chars:
        return text[:max_chars] + "…"
    return text


def gc_type_breakdown(*, top_n: int = 20) -> "list[TypeBreakdownRow]":
    """#5959 ⓐ: every live object ``gc`` can see, grouped by
    ``type(obj).__name__``, ranked by TOTAL ``sys.getsizeof`` across the
    type (not count alone — a million tiny ints rank below one type
    holding gigabytes, which is the actual question this answers).

    No curated list of "types reyn cares about" — the population is
    whatever ``gc.get_objects()`` returns, full stop. ``sys.getsizeof``
    is SHALLOW (a container's own size, not its contents' — see module
    docstring); this is a RANKING tool, not an exact accounting.

    A single object whose own ``sys.getsizeof`` call raises (rare, but
    not impossible for a foreign C-extension type) is skipped for THAT
    object rather than aborting the whole scan — one uncooperative type
    must not blind this tool to every other one."""
    totals: "dict[str, list[int]]" = {}  # type_name -> [count, total_bytes]
    for obj in gc.get_objects():
        type_name = type(obj).__name__
        try:
            size = sys.getsizeof(obj)
        except Exception:  # noqa: BLE001 - a foreign type's __sizeof__ may raise anything
            continue
        bucket = totals.setdefault(type_name, [0, 0])
        bucket[0] += 1
        bucket[1] += size
    rows = [
        TypeBreakdownRow(type_name=name, count=count, total_sizeof_bytes=total)
        for name, (count, total) in totals.items()
    ]
    rows.sort(key=lambda r: r.total_sizeof_bytes, reverse=True)
    return rows[:top_n]


def gc_largest_objects(*, top_n: int = 20) -> "list[LargestObjectRow]":
    """#5959 ⓐ (sibling view): the single largest live objects by
    ``sys.getsizeof``, individually — a type-level aggregate
    (:func:`gc_type_breakdown`) can hide one enormous outlier inside a
    type with a small average, and vice versa; this view answers the
    complementary question."""
    rows: "list[LargestObjectRow]" = []
    for obj in gc.get_objects():
        try:
            size = sys.getsizeof(obj)
        except Exception:  # noqa: BLE001 - a foreign type's __sizeof__ may raise anything
            continue
        rows.append(
            LargestObjectRow(
                type_name=type(obj).__name__,
                sizeof_bytes=size,
                repr_snippet=_safe_repr_snippet(obj),
            ),
        )
    rows.sort(key=lambda r: r.sizeof_bytes, reverse=True)
    return rows[:top_n]


def bounding_axis_census(
    *, measurement_providers: "dict[str, MeasurementProvider] | None" = None,
) -> "list[BoundingCensusRow]":
    """#5959 ⓑ: every config field carrying ``Axis.BOUNDING`` — the
    "someone declared this resource should be bounded" registry — DERIVED
    from :func:`reyn.config.config_schema.walk_config_schema`, never a
    second, hand-maintained list (architect's explicit acceptance:
    "``Axis.BOUNDING`` を持つ field を 1 つ足すと、profiler の出力に自動で
    現れる" — verified in
    ``tests/runtime/test_5959_memory_breakdown.py``'s own strip witness).

    ``measurement_providers`` is a caller-supplied ``{config_key:
    MeasurementProvider}`` map — THIS module wires none by default (it has
    no access to a live ``Session``/registry on its own; a caller with one
    passes its own providers). A field with no registered provider, or
    whose provider itself returns ``None``, reports ``measured_bytes=
    None`` — rendered ``unmeasured`` everywhere this surfaces, never
    silently shown as ``0``."""
    from reyn.config.config_schema import walk_config_schema
    from reyn.config_axis import Axis

    providers = measurement_providers or {}
    rows: "list[BoundingCensusRow]" = []
    for node in walk_config_schema():
        if node.axis != Axis.BOUNDING:
            continue
        provider = providers.get(node.key)
        measured = provider() if provider is not None else None
        rows.append(
            BoundingCensusRow(key=node.key, declared_cap=node.default, measured_bytes=measured),
        )
    return rows


def collect_memory_breakdown(
    *, top_n: int = 20, measurement_providers: "dict[str, MeasurementProvider] | None" = None,
) -> MemoryBreakdown:
    """The one bundling call both ``reyn doctor-memory`` and the
    ``process_memory_breakdown`` audit-event emit use — same
    ``gc.get_objects()`` pass backing both tables (:func:`gc_type_
    breakdown`/:func:`gc_largest_objects` each re-walk independently
    today; a future perf pass could share one pass between them, left
    unmerged here since correctness does not depend on it and #5959's own
    acceptance is about the OUTPUT shape, not walk count)."""
    return MemoryBreakdown(
        type_breakdown=gc_type_breakdown(top_n=top_n),
        largest_objects=gc_largest_objects(top_n=top_n),
        bounding_census=bounding_axis_census(measurement_providers=measurement_providers),
    )


def emit_memory_breakdown_event(emit: "Callable[..., None]", breakdown: MemoryBreakdown) -> None:
    """#5959: the ONE place ``process_memory_breakdown`` is built as an
    audit-event payload — so the CLI's ``emit_cli_event`` seam and a
    future live-session seam (#5959's own ② stage, not wired in this PR
    — see module docstring) always produce byte-identical field shapes.
    ``emit`` is any ``(kind, **payload) -> None`` callable (``EventLog.
    emit``, ``emit_cli_event``, or a test double) — this function never
    imports a concrete emitter itself, mirroring ``Session.
    _emit_process_footprint``'s own caller-supplied-sink shape."""
    emit(
        "process_memory_breakdown",
        type_breakdown=[
            {"type": r.type_name, "count": r.count, "total_sizeof_bytes": r.total_sizeof_bytes}
            for r in breakdown.type_breakdown
        ],
        largest_objects=[
            {"type": r.type_name, "sizeof_bytes": r.sizeof_bytes, "repr_snippet": r.repr_snippet}
            for r in breakdown.largest_objects
        ],
        bounding_census=[
            {
                "key": r.key,
                "declared_cap": r.declared_cap if _is_json_scalar(r.declared_cap) else repr(r.declared_cap),
                "measured_bytes": r.measured_bytes,
            }
            for r in breakdown.bounding_census
        ],
        estimate_note=(
            "sys.getsizeof is SHALLOW -- a container reports its own size, "
            "not its contents'. This is a SUSPECT ranking, not an exact "
            "accounting."
        ),
    )


def _is_json_scalar(value: object) -> bool:
    """True when *value* is directly JSON-serialisable as a scalar
    (``str``/``int``/``float``/``bool``/``None``) -- a config default can
    be a ``Literal`` string, a dataclass instance, or other non-scalar the
    audit-event payload must not choke on; anything else is repr'd
    instead (see this function's own caller)."""
    return value is None or isinstance(value, (str, int, float, bool))


def tracemalloc_is_armed() -> bool:
    """#5959 stage ③: whether ``REYN_MEMORY_TRACE`` was set in THIS
    process's environment. A pure ``os.environ`` read -- never itself
    imports or touches ``tracemalloc``, so calling this costs nothing
    even when tracing was never armed."""
    return bool(os.environ.get(TRACEMALLOC_ENV_VAR))


def arm_tracemalloc_if_requested() -> bool:
    """#5959 stage ③: called ONCE, from the CLI's own ``main()`` entry
    point, before any other import does real work -- tracemalloc's
    per-allocation overhead only attributes allocations that happen
    AFTER it starts, so arming it any later already misses whatever
    already ran. Returns whether it armed. When ``REYN_MEMORY_TRACE`` is
    unset, this function does not import ``tracemalloc`` at all -- zero
    tracemalloc calls executed, not "started then immediately idle" (the
    opt-in's whole point: an operator who never asked for this pays
    nothing, not even the import)."""
    if not tracemalloc_is_armed():
        return False
    import tracemalloc

    if not tracemalloc.is_tracing():
        tracemalloc.start()
    return True


def tracemalloc_top_allocations(*, top_n: int = 20) -> "list[dict] | None":
    """#5959 stage ③: the top *top_n* allocation sites by current traced
    size, each carrying the allocating file:line -- deep-dive detail
    :func:`gc_type_breakdown` cannot give (a type name, not a call site).
    Returns ``None`` (never an empty list masquerading as "measured, zero
    found") when tracemalloc was never armed for this process -- a
    caller must not read ``None`` and ``[]`` as the same fact."""
    import tracemalloc

    if not tracemalloc.is_tracing():
        return None
    snapshot = tracemalloc.take_snapshot()
    stats = snapshot.statistics("lineno")[:top_n]
    return [
        {
            "file_line": str(stat.traceback[0]) if stat.traceback else "<unknown>",
            "size_bytes": stat.size,
            "count": stat.count,
        }
        for stat in stats
    ]
