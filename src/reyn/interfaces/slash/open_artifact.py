"""/open — open a generated artifact with the OS's own default app.

#4482 PR-3. Takes the `ref` displayed on the artifact's list row and
NOTHING else — architect's #4482 ruling: "開くのに使う path そのものを表示
し、表示から実行まで同じ path を使う" (show the exact path used to open,
and use that SAME path to execute). This command carries a ref, never a
raw path (the artifact payload itself never puts a raw filesystem path on
the wire — `artifact_payload.py`'s own invariant 1), and the client-side
handler resolves that SAME ref to a path and opens exactly that.

Sentinel-forwarding, matching `/copy`'s own established shape: this
command's whole job is parsing the argument and forwarding it as an
OutboxMessage the TUI's own output loop intercepts — the actual ref
resolution + OS-launch happens client-side (needs `project_root`/
`agent_name`, both readily available there, and "launch a local
application" only makes sense on the machine the user is sitting at).

Usage::

    /open <ref>
"""
from __future__ import annotations

from reyn.interfaces.slash import SlashContext, slash
from reyn.runtime.outbox import OutboxMessage


@slash(
    "open",
    summary="Open a generated artifact with the OS default app",
    locus="client",
    usage="/open <ref>",
)
async def open_cmd(ctx: "SlashContext", args: str) -> None:
    ctx.transport.put_display(OutboxMessage(
        kind="__open_artifact__", text=(args or "").strip(),
    ))
