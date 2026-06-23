"""``/reload`` — hot-reload the runtime IN-set config at the next turn boundary (#2073).

Schedules a config hot-reload: the runtime-mutable IN-set (``.reyn/*.yaml`` —
MCP servers, cron jobs, …) is re-read and reapplied at the **turn boundary**
(finish-reason=stop), so the next turn runs under the new config (1 turn = 1 config
snapshot, never mid-turn).

The startup OUT-set (``reyn.yaml`` — security / permissions / sandbox / budget / the
loop valve) is **restart-only** and is never touched by a hot-reload — the file-split
is the structural write-gate boundary.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from reyn.interfaces.slash import reply, reply_error, slash

if TYPE_CHECKING:
    from reyn.runtime.session import Session


@slash(
    "reload",
    summary="Hot-reload runtime config (.reyn/*.yaml) at the next turn boundary",
    usage="/reload",
    see_also=("docs/concepts/runtime/permission-model.md",),
)
async def reload_cmd(session: "Session", args: str) -> None:
    """``/reload`` — schedule a runtime config hot-reload.

    The IN-set (``.reyn/*.yaml``) is re-read + reapplied at the next turn boundary;
    the startup ``reyn.yaml`` is restart-only. Fail-loud if the reloader is absent.
    """
    reloader = getattr(session, "_hot_reloader", None)
    if reloader is None:
        await reply_error(
            session,
            "config hot-reload is not available in this session "
            "(no hot-reloader wired).",
        )
        return

    reloader.request_reload(source="operator")
    await reply(
        session,
        "✓ Config reload scheduled — the runtime IN-set (.reyn/*.yaml) is re-read "
        "and reapplied at the next turn boundary. (Startup config in reyn.yaml is "
        "restart-only.)",
    )
