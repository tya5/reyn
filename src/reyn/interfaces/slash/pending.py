"""/pending slash command — observe / discard / claim stalled operations.

Issue #277 (= #270 First instance, #268 Phase 1 follow-up TUI surface).

Sub-commands:
  /pending             — alias of ``/pending list``
  /pending list        — print the stalled iv table (kind / id / origin / age / summary)
  /pending discard <id> — discard a stalled iv (= sets future to refusal)
  /pending claim <id>   — rebind origin to this TUI channel + re-dispatch

The 3 operations match the #270 framework vocabulary (= observe / discard /
claim). All routed through ``Session.{list_stalled_interventions,
discard_pending_intervention, claim_pending_intervention}`` introduced in
PR #275.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from reyn.interfaces.slash import reply, reply_error, slash

if TYPE_CHECKING:
    from reyn.runtime.session import Session


_USAGE = (
    "Usage: /pending [list | discard <id> | claim <id>]\n"
    "  list           — print stalled / cross-channel pending operations\n"
    "  discard <id>   — discard a stalled iv (refusal future)\n"
    "  claim <id>     — claim a stalled iv to this TUI channel\n"
    "                   (id: short or full intervention_id from /pending list)"
)
_NO_SESSION = (
    "/pending only works inside `reyn chat`; no session attached."
)


def _render_list(pending_ops: list) -> str:
    """Render a stalled-op list as plain text for the conv pane."""
    if not pending_ops:
        return "no pending operations"
    lines = [
        f"{len(pending_ops)} pending operation"
        + ("s" if len(pending_ops) != 1 else "")
        + ":",
    ]
    for v in pending_ops:
        # PendingOpView dataclass — attribute access. Defensively
        # check via getattr in case caller passes a dict-shaped
        # mock (= test path).
        kind = getattr(v, "kind", None) or (
            v.get("kind", "?") if isinstance(v, dict) else "?"
        )
        iv_id = getattr(v, "id", None) or (
            v.get("id", "") if isinstance(v, dict) else ""
        )
        origin = getattr(v, "origin_channel_id", None) or (
            v.get("origin_channel_id", "") if isinstance(v, dict) else ""
        )
        summary = getattr(v, "summary", None) or (
            v.get("summary", "") if isinstance(v, dict) else ""
        )
        iv_id_short = str(iv_id)[:8]
        # Two lines per entry — keeps wide stuff visible without
        # wrapping into a ragged blob.
        lines.append(f"  {kind:<14} {iv_id_short}  ({origin})")
        if summary:
            lines.append(f"      ↳ {summary[:60]}")
    return "\n".join(lines)


def _resolve_iv_id(
    session: "Session", supplied: str,
) -> tuple[str | None, str | None]:
    """Resolve a possibly-short iv id to the full one in the stalled list.

    Returns ``(resolved_id, error_message)``. ``resolved_id`` is the
    full id when exactly one stalled iv has a prefix-matching id;
    ``error_message`` is non-None on no-match or ambiguous-match.
    """
    supplied = supplied.strip()
    if not supplied:
        return None, "missing intervention id"
    try:
        pending_ops = session.list_stalled_interventions()
    except Exception as exc:
        return None, f"list_stalled_interventions failed: {exc}"
    candidates = [
        v for v in pending_ops
        if str(getattr(v, "id", "")).startswith(supplied)
    ]
    if not candidates:
        return None, f"no stalled intervention with id starting {supplied!r}"
    if len(candidates) > 1:
        ids = ", ".join(str(getattr(c, "id", ""))[:12] for c in candidates)
        return None, f"ambiguous id {supplied!r} — matches: {ids}"
    return str(getattr(candidates[0], "id", "")), None


@slash(
    "pending",
    summary=(
        "List / discard / claim stalled cross-channel ops "
        "(subcommands: list | discard <id> | claim <id>)"
    ),
    usage="/pending [list|discard <id>|claim <id>]",
)
async def pending_cmd(session: "Session", args: str) -> None:
    """Dispatch ``/pending [list|discard|claim]`` subcommands."""
    parts = args.strip().split(maxsplit=1)
    if not parts or parts[0] == "list":
        await _list(session)
        return
    sub = parts[0]
    sub_args = parts[1] if len(parts) > 1 else ""
    if sub == "discard":
        await _discard(session, sub_args)
    elif sub == "claim":
        await _claim(session, sub_args)
    else:
        await reply_error(session, _USAGE)



async def _list(session: "Session") -> None:
    if not hasattr(session, "list_stalled_interventions"):
        await reply_error(session, _NO_SESSION)
        return
    try:
        ops = session.list_stalled_interventions()
    except Exception as exc:
        await reply_error(session, f"/pending list failed: {exc}")
        return
    await reply(session, _render_list(ops))


async def _discard(session: "Session", supplied_id: str) -> None:
    """Two-step confirm: first invocation shows a warning; second executes.

    Mirrors ``/reset``'s pattern (Wave-13 B#2).  The user must re-type
    ``/pending discard <id> confirm`` to proceed.  The ``confirm`` suffix
    is stripped before resolving the intervention id so the existing
    prefix-resolution logic is unchanged.
    """
    if not hasattr(session, "discard_pending_intervention"):
        await reply_error(session, _NO_SESSION)
        return

    # Detect "confirm" suffix (case-insensitive, space-separated).
    stripped = supplied_id.strip()
    if stripped.lower().endswith(" confirm"):
        id_part = stripped[: -len(" confirm")].strip()
        _do_confirm = True
    else:
        id_part = stripped
        _do_confirm = False

    iv_id, err = _resolve_iv_id(session, id_part)
    if err is not None:
        await reply_error(session, err)
        return
    assert iv_id is not None  # mypy guard

    if not _do_confirm:
        # First invocation — show warning with iv context, require confirm.
        # Retrieve iv details from the stalled list for the warning line.
        kind_hint = ""
        skill_hint = ""
        try:
            ops = session.list_stalled_interventions()
            match = next(
                (v for v in ops if str(getattr(v, "id", "")).startswith(id_part)),
                None,
            )
            if match is not None:
                kind_hint = getattr(match, "kind", "") or ""
                skill_hint = getattr(match, "summary", "") or ""
        except Exception:  # noqa: BLE001 — best-effort
            pass
        context = ""
        if kind_hint:
            context = f" ({kind_hint}"
            if skill_hint:
                context += f": {skill_hint[:40]}"
            context += ")"
        await reply(
            session,
            f"⚠ About to discard pending intervention: {iv_id[:8]}{context}\n"
            f"Type `/pending discard {id_part} confirm` to proceed, "
            "or anything else to leave it queued.",
        )
        return

    try:
        ok = await session.discard_pending_intervention(iv_id)
    except Exception as exc:
        await reply_error(session, f"discard failed: {exc}")
        return
    if ok:
        await reply(session, f"discarded {iv_id[:8]}")
    else:
        await reply_error(session, f"discard {iv_id[:8]}: not in stalled queue")


async def _claim(session: "Session", supplied_id: str) -> None:
    if not hasattr(session, "claim_pending_intervention"):
        await reply_error(session, _NO_SESSION)
        return
    iv_id, err = _resolve_iv_id(session, supplied_id)
    if err is not None:
        await reply_error(session, err)
        return
    assert iv_id is not None
    # Use the canonical REPL listener channel so the re-dispatched iv is NOT
    # immediately re-parked stalled by InterventionCoordinator (it checks
    # has_listener(iv.origin_channel_id) and only routes through
    # InterventionHandler when the claimed channel matches a registered listener).
    # The old f"tui:{agent_name}" was the Textual TUI's per-agent convention;
    # the current REPL registers DEFAULT_CHAT_CHANNEL_ID ("tui").
    from reyn.runtime.session import DEFAULT_CHAT_CHANNEL_ID
    channel_id = DEFAULT_CHAT_CHANNEL_ID
    try:
        view = await session.claim_pending_intervention(iv_id, channel_id)
    except Exception as exc:
        await reply_error(session, f"claim failed: {exc}")
        return
    if view is None:
        await reply_error(
            session, f"claim {iv_id[:8]}: not in stalled queue",
        )
        return
    summary = getattr(view, "summary", "") or ""
    await reply(
        session,
        f"claimed {iv_id[:8]} to {channel_id}"
        + (f": {summary[:60]}" if summary else ""),
    )
