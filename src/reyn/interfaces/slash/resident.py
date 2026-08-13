"""/resident slash command (#4497 Phase 1).

Reports approximate resident-memory footprint of the major in-process
containers #4497's issue enumerates as unmeasured — count + shallow
`sys.getsizeof` estimate, per container. No threshold, no eviction: this
exists to give an operator, next time RSS is high, a way to answer "what's
actually dominant" without guessing.

Deliberately a slash command, not a `reyn media stats`-style CLI
subcommand (#4485/#4488): the containers this measures only exist inside
the ONE process that's actually holding that memory — a fresh CLI process
started to "measure" would just see empty state. Naming it to match the
disk-stats CLI surface was considered and rejected (lead-coder's own
correction on #4497) — same name for two different measurement domains is
the exact "same name, two meanings" defect class this repo has hit
repeatedly.

All the actual attribute-reading lives in `reyn.runtime.resident_stats`
(outside this package) — this handler only calls it and formats the
result, keeping the slash layer thin and avoiding the private-`Session`-
member ratchet `tests/interfaces/test_3595_s4_slash_handler_seam.py`
enforces for this package specifically (this command never spells
`ctx.session._x` itself; `resident_stats.py` does, one layer down, as a
reusable/independently-testable measurement module).
"""
from __future__ import annotations

from reyn.interfaces.slash import SlashContext, reply, slash
from reyn.runtime.resident_stats import (
    ContainerStat,
    process_global_container_stats,
    session_container_stats,
)


def _format_row(stat: "ContainerStat") -> str:
    kb = stat.approx_bytes / 1024
    return f"  {stat.name:<45} {stat.count:>8,} items  ~{kb:>10,.1f} KB"


def _render(session_stats: "list[ContainerStat]", process_stats: "list[ContainerStat]") -> str:
    lines = [
        "Resident-memory containers (#4497 Phase 1 — count + approximate "
        "bytes, shallow sys.getsizeof, no threshold, no eviction):",
        "",
        "Session-lifetime:",
    ]
    lines.extend(_format_row(s) for s in session_stats)
    lines.append("")
    lines.append("Process-global (aggregate across every session this process has touched):")
    lines.extend(_format_row(s) for s in process_stats)
    return "\n".join(lines)


@slash(
    "resident",
    summary="Approximate resident-memory breakdown by container (#4497)",
)
async def resident_cmd(ctx: "SlashContext", args: str) -> None:  # noqa: ARG001 — no args yet
    """``/resident`` — count + approximate bytes for the major in-process
    containers, so an operator seeing high RSS can see what's dominant."""
    session_stats = session_container_stats(ctx.session)
    process_stats = process_global_container_stats()
    await reply(ctx, _render(session_stats, process_stats))
