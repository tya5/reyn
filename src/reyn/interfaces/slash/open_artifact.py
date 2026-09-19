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

#6230 stage 1 (issue thread ruling, architect + lead-coder): ``text`` is the
standard AG-UI channel (``{"text": text}``, reaches a client that carries
ZERO reyn-specific rendering) and MUST carry a genuinely human-readable
representation, never the control value a reyn-aware consumer needs to
act — that control value (the ref) now lives in ``meta["ref"]``, the
private channel only a reyn-aware client reads.

Before this stage ``text`` carried the raw ref itself — a value meant for
:func:`~reyn.data.workspace.artifact_ref.resolve_ref`, not a human. A
surface with no reyn-specific handler already printed that raw ref
verbatim, which was never a prepared fallback representation, only a
leaked control value (#6230 issue thread).

**Wire-meaning change, disclosed (not fixed — architect ruling, #6230):**
an OLDER client built against the pre-stage-1 contract still reads ``text``
AS IF it were the ref and would try to treat this new human sentence as
one — visibly (it fails to resolve, never silently), never silently, which
is this arc's own standard ("見えて間違う方が良い" — better to fail
visibly than silently do nothing). This naturally stops mattering once
that client is rebuilt against the post-stage-1 contract. Nobody has
checked whether such an older client actually exists outside this
codebase; this note states that plainly rather than implying it was
verified.

``__open_artifact__`` is control-filtered (never forwarded to the AG-UI
wire at all — see ``outbox.py``'s ``CONTROL_KINDS`` entry), so in practice
today no REMOTE client of any vintage ever receives this sentinel; the
degrade above only matters for an in-process consumer built directly
against the OutboxMessage shape.

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
    ref = (args or "").strip()
    ctx.transport.put_display(OutboxMessage(
        kind="__open_artifact__",
        text=f"artifact ref: {ref}" if ref else "no artifact ref given",
        meta={"ref": ref},
    ))
