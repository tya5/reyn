"""/list, /answer slash commands."""
from __future__ import annotations

from reyn.interfaces.slash import SlashContext, reply, reply_error, slash


@slash("list", summary="List pending interventions")
async def list_cmd(ctx: "SlashContext", args: str) -> None:
    """``/list`` — show pending interventions."""
    lines: list[str] = ["running tasks: (none)"]
    active_ivs = ctx.session._interventions.list_active()
    if active_ivs:
        lines.append("pending interventions:")
        for iv in active_ivs:
            short = (iv.run_id[-4:] if iv.run_id else "----")
            lines.append(
                f"  {iv.id[:8]}  {iv.kind:<20}  "
                f"{iv.actor or '?'}#{short}"
            )
    await reply(ctx, "\n".join(lines))


def _intervention_id_completer(
    source: "object", arg_partial: str = "",
) -> list[str]:
    """Wave-11 C#3 — completer for /answer <id-prefix> <text>.

    ``source`` is a ``CompletionSourceSnapshot | None`` (#5044) — a plain
    value, never a live ``Session``. Surfaces
    :attr:`~reyn.interfaces.repl.read_model.CompletionSourceSnapshot.
    active_intervention_ids` so the user can Tab through them — the ONE
    genuinely per-turn-mutable field among the snapshot's sources (that
    class's own docstring). ``/answer`` takes ``<id-prefix> <text>`` —
    only the FIRST word is the id; once the user has typed past the
    space the input is the answer body and the picker hint is
    irrelevant. We filter by the LAST whitespace-delimited token
    of ``arg_partial`` so the prefix-match still works regardless
    of where the cursor sits.
    """
    ids = getattr(source, "active_intervention_ids", None)
    if not ids:
        return []
    ids = list(ids)
    if not arg_partial:
        return ids
    # If the user has already typed past the first whitespace,
    # the picker hint is no longer useful (= they're typing the
    # answer body, not the id). The empty-match list naturally
    # falls back to ``set_hint`` upstream.
    if " " in arg_partial:
        return []
    return [iid for iid in ids if iid.startswith(arg_partial)]


@slash(
    "answer",
    summary="Answer a pending intervention",
    usage="/answer <id-prefix> <text>",
    completer=_intervention_id_completer,
)
async def answer_cmd(ctx: "SlashContext", args: str) -> None:
    """``/answer <id-prefix> <text>`` — deliver answer to a non-head
    intervention.

    The "head" intervention (= the one the TUI currently shows) is
    answered by a plain text submission; this command targets the rest
    of the queue, identified by prefix.
    """
    parts = args.split(maxsplit=1)
    if not parts:
        await reply_error(ctx, "usage: /answer <id-prefix> <text>")
        return
    prefix = parts[0]
    text = parts[1] if len(parts) > 1 else ""
    iid, candidates = ctx.session._resolve_intervention_id(prefix)
    if iid is None:
        if not candidates:
            await reply_error(
                ctx,
                f"no pending intervention matches {prefix!r}",
            )
        else:
            matches = ", ".join(c[:8] for c in candidates)
            await reply_error(
                ctx,
                f"ambiguous prefix {prefix!r}; matches: {matches}",
            )
        return
    iv = ctx.session._interventions.get(iid)
    if iv is None:
        await reply_error(
            ctx,
            f"intervention {prefix!r} disappeared mid-resolution",
        )
        return
    await ctx.session._deliver_answer_to(iv, text)
