"""/list, /cancel, /answer slash commands.

Migrated out of ``session.py`` per the cli-redesign plan (`docs/deep-dives/
contributing/cli-redesign.md`). Helpers ``_run_short`` / ``_run_meta``
remain in ``session`` as module-level utilities (used by other call sites
too); we import them here.
"""
from __future__ import annotations

import time
from typing import TYPE_CHECKING

from reyn.chat.outbox import OutboxMessage
from reyn.chat.session import _run_meta, _run_short
from reyn.chat.slash import reply, reply_error, slash

if TYPE_CHECKING:
    from reyn.chat.session import ChatSession


@slash("list", summary="List running skills and pending interventions")
async def list_cmd(session: "ChatSession", args: str) -> None:
    """``/list`` — show running skill tasks + pending interventions."""
    now = time.monotonic()
    lines: list[str] = []
    if session.running_skills:
        lines.append("running skills:")
        for rid in session.running_skills:
            started = session.running_skills_started_at.get(rid)
            elapsed = f"{int(now - started)}s" if started is not None else "?s"
            # Recover skill_name from the run_id format
            # ``TIMESTAMP_<skill>_<short>`` — split between first and last
            # underscore.
            short = _run_short(rid)
            trimmed = rid[: -len(short) - 1] if short else rid  # drop "_abcd"
            # trimmed = "TIMESTAMP_skill_name"; drop the leading TIMESTAMP_
            _, _, skill_part = trimmed.partition("_")
            lines.append(
                f"  {short}  {skill_part:<24} {elapsed:>5}  (run_id={rid})"
            )
    else:
        lines.append("running skills: (none)")
    active_ivs = session._interventions.list_active()
    if active_ivs:
        lines.append("pending interventions:")
        for iv in active_ivs:
            short = (iv.run_id[-4:] if iv.run_id else "----")
            lines.append(
                f"  {iv.id[:8]}  {iv.kind:<20}  "
                f"{iv.skill_name or '?'}#{short}"
            )
    await reply(session, "\n".join(lines))


@slash("cancel", summary="Cancel a running skill: /cancel <id-prefix>")
async def cancel_cmd(session: "ChatSession", args: str) -> None:
    """``/cancel <id-prefix>`` — cancel a running skill task."""
    prefix = args.strip()
    if not prefix:
        await reply_error(session, "usage: /cancel <id-prefix>")
        return
    rid, candidates = session._resolve_run_id(prefix)
    if rid is None:
        if not candidates:
            await reply_error(session, f"no running skill matches {prefix!r}")
        else:
            matches = ", ".join(_run_short(c) for c in candidates)
            await reply_error(
                session,
                f"ambiguous prefix {prefix!r}; matches: {matches}",
            )
        return
    task = session.running_skills.get(rid)
    if task is None or task.done():
        await reply(session, f"skill {_run_short(rid)} already finished")
        return
    task.cancel()
    # Preserve the per-run meta on the cancel-requested system message so
    # the TUI's skill-activity row can match against it.
    await session._put_outbox(OutboxMessage(
        kind="system",
        text="cancel requested",
        meta=_run_meta(rid, None),
    ))


@slash(
    "answer",
    summary="Answer a pending intervention: /answer <id-prefix> <text>",
)
async def answer_cmd(session: "ChatSession", args: str) -> None:
    """``/answer <id-prefix> <text>`` — deliver answer to a non-head
    intervention.

    The "head" intervention (= the one the TUI currently shows) is
    answered by a plain text submission; this command targets the rest
    of the queue, identified by prefix.
    """
    parts = args.split(maxsplit=1)
    if not parts:
        await reply_error(session, "usage: /answer <id-prefix> <text>")
        return
    prefix = parts[0]
    text = parts[1] if len(parts) > 1 else ""
    iid, candidates = session._resolve_intervention_id(prefix)
    if iid is None:
        if not candidates:
            await reply_error(
                session,
                f"no pending intervention matches {prefix!r}",
            )
        else:
            matches = ", ".join(c[:8] for c in candidates)
            await reply_error(
                session,
                f"ambiguous prefix {prefix!r}; matches: {matches}",
            )
        return
    iv = session._interventions.get(iid)
    if iv is None:
        await reply_error(
            session,
            f"intervention {prefix!r} disappeared mid-resolution",
        )
        return
    await session._deliver_answer_to(iv, text)
