"""`reyn doctor-memory` — #5959 (owner-hit): the in-process answer to "who
is holding this" a leak suspicion needs. A SEPARATE top-level command from
``reyn doctor`` (D-1/D-2/D-3's own single flat report, ``doctor.py``) rather
than a mode of it — this reads ``gc.get_objects()`` (an operation with real
CPU cost, unlike every existing ``doctor`` check's cheap stat/probe reads)
and would otherwise silently widen what "just run doctor" costs. Registered
as its own subcommand, hyphenated, matching ``support-bundle``'s own
multi-word convention. Real logic lives in
:mod:`reyn.runtime.memory_breakdown` — this module is CLI wiring + the P6
emit only.

D-1 (measure, never assert) still applies here, unchanged from ``doctor``'s
own ruling: every number below comes from a live ``gc.get_objects()``
walk or ``walk_config_schema()``'s real, current schema — never a restated
config value dressed up as a measurement.
"""
from __future__ import annotations

import argparse

from reyn.runtime.memory_breakdown import (
    MemoryBreakdown,
    collect_memory_breakdown,
    emit_memory_breakdown_event,
    tracemalloc_is_armed,
    tracemalloc_top_allocations,
)


def register(sub) -> None:
    p = sub.add_parser(
        "doctor-memory",
        help="In-process memory breakdown (suspect ranking + bounded-resource census) for leak triage",
    )
    p.add_argument(
        "--top-n", type=int, default=20,
        help="How many rows to show per discovery table (default: 20).",
    )
    p.add_argument(
        "--no-audit-event", action="store_true",
        help="Skip emitting the process_memory_breakdown audit-event (report to stdout only).",
    )
    p.set_defaults(func=run)


def _print_breakdown(breakdown: MemoryBreakdown) -> None:
    print(
        "reyn doctor-memory — SUSPECT ranking, not an exact accounting "
        "(sys.getsizeof is shallow: a container's own size, never its "
        "contents')."
    )
    print(
        "⚠️  ⓐ cannot see a large str/bytes/bytearray held via a reference "
        "AT ALL (gc.get_objects() never tracks those types) — if you "
        "suspect a Python object is holding a big STRING, use "
        "REYN_MEMORY_TRACE=1 (③ below), not ⓐ."
    )
    print()
    print(f"ⓐ discovery — top {len(breakdown.type_breakdown)} types by total sys.getsizeof:")
    for type_row in breakdown.type_breakdown:
        print(
            f"  {type_row.type_name:<32} count={type_row.count:<8} "
            f"total={type_row.total_sizeof_bytes:>12,} bytes",
        )
    print()
    print(f"ⓐ discovery — top {len(breakdown.largest_objects)} single largest objects:")
    for obj_row in breakdown.largest_objects:
        print(f"  {obj_row.type_name:<32} {obj_row.sizeof_bytes:>12,} bytes  {obj_row.repr_snippet}")
    print()
    print(
        f"ⓑ completeness — {len(breakdown.bounding_census)} Axis.BOUNDING "
        "config field(s) (derived from walk_config_schema(), never a "
        "hand-written list):"
    )
    for census_row in breakdown.bounding_census:
        measured = (
            "unmeasured" if census_row.measured_bytes is None
            else f"{census_row.measured_bytes:,} bytes"
        )
        print(f"  {census_row.key:<48} declared_cap={census_row.declared_cap!r:<20} measured={measured}")
    print()
    dead = breakdown.unbounded_or_dead_declarations
    print(
        f"⚠️  {len(dead)} Axis.BOUNDING field(s) read 0 or unmeasured "
        "(type A dead-declaration candidates — an unmeasured row means "
        "no live reading is wired yet, NOT confirmed dead):"
    )
    for dead_row in dead:
        measured = "unmeasured" if dead_row.measured_bytes is None else "0"
        print(f"  {dead_row.key:<48} measured={measured}")


def run(args: argparse.Namespace) -> None:
    breakdown = collect_memory_breakdown(top_n=args.top_n)
    _print_breakdown(breakdown)
    if not args.no_audit_event:
        from reyn.core.events.events import emit_cli_event

        emit_memory_breakdown_event(emit_cli_event, breakdown)
    print()
    if tracemalloc_is_armed():
        allocations = tracemalloc_top_allocations(top_n=args.top_n)
        print(f"③ tracemalloc — top {len(allocations or [])} allocation site(s) (REYN_MEMORY_TRACE armed):")
        for alloc in allocations or []:
            print(f"  {alloc['file_line']:<64} {alloc['size_bytes']:>12,} bytes  ({alloc['count']} block(s))")
    else:
        print(
            "③ tracemalloc — not armed (set REYN_MEMORY_TRACE=1 at process "
            "start for allocation-site detail; must be set before the "
            "process that leaked, not this doctor-memory invocation)."
        )
