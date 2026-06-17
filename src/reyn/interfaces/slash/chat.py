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
from reyn.interfaces.slash import reply, reply_error, slash

if TYPE_CHECKING:
    from reyn.chat.session import Session


@slash("list", summary="List running skills and pending interventions")
async def list_cmd(session: "Session", args: str) -> None:
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


def _running_run_id_completer(
    session: "object", arg_partial: str = "",
) -> list[str]:
    """Wave-11 C#3 — completer for /cancel <id-prefix>.

    Surfaces running ``run_id`` keys from ``session.running_skills``
    so the user can Tab through them in the picker hint instead of
    eye-balling them from ``/list`` output. Defensive: empty list
    on any access failure (= test stubs / pre-init session) so a
    broken completer can't break the picker.
    """
    try:
        running = list(getattr(session, "running_skills", {}).keys())
    except Exception:
        return []
    if not arg_partial:
        return running
    last_word = arg_partial.rsplit(" ", 1)[-1] if " " in arg_partial else arg_partial
    return [rid for rid in running if rid.startswith(last_word)]


@slash(
    "cancel",
    summary="Cancel a running skill",
    usage="/cancel <id-prefix> [confirm]",
    completer=_running_run_id_completer,
)
async def cancel_cmd(session: "Session", args: str) -> None:
    """``/cancel <id-prefix>`` — cancel a running skill task (2-step confirm).

    First invocation prints a warning and asks the user to re-type with
    ``confirm`` appended.  Second invocation (``/cancel <id-prefix> confirm``)
    executes the cancellation.  Mirrors ``/reset``'s 2-step pattern so a
    Tab-completed prefix can't accidentally abort the wrong skill on first
    press (Wave-13 B#2).
    """
    stripped = args.strip()
    # Detect "confirm" suffix (case-insensitive, space-separated).
    if stripped.lower().endswith(" confirm"):
        prefix = stripped[: -len(" confirm")].strip()
        _do_confirm = True
    else:
        prefix = stripped
        _do_confirm = False

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

    if not _do_confirm:
        # First invocation — show warning, require explicit confirm.
        short = _run_short(rid)
        # Recover skill_name from run_id for the warning context line.
        trimmed = rid[: -len(short) - 1] if short else rid
        _, _, skill_part = trimmed.partition("_")
        await reply(
            session,
            f"⚠ About to cancel: {skill_part} #{short}\n"
            f"Type `/cancel {prefix} confirm` to abort the skill, "
            "or anything else to leave it running.",
        )
        return

    task.cancel()
    # Preserve the per-run meta on the cancel-requested system message so
    # the TUI's skill-activity row can match against it.
    await session._put_outbox(OutboxMessage(
        kind="system",
        text="cancel requested",
        meta=_run_meta(rid, None),
    ))


def _intervention_id_completer(
    session: "object", arg_partial: str = "",
) -> list[str]:
    """Wave-11 C#3 — completer for /answer <id-prefix> <text>.

    Surfaces active intervention ids from
    ``session._interventions.list_active()`` so the user can Tab
    through them. ``/answer`` takes ``<id-prefix> <text>`` — only
    the FIRST word is the id; once the user has typed past the
    space the input is the answer body and the picker hint is
    irrelevant. We filter by the LAST whitespace-delimited token
    of ``arg_partial`` so the prefix-match still works regardless
    of where the cursor sits.
    """
    try:
        interventions = getattr(session, "_interventions", None)
        if interventions is None:
            return []
        ids = [iv.id for iv in interventions.list_active()]
    except Exception:
        return []
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
async def answer_cmd(session: "Session", args: str) -> None:
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
