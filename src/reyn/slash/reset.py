"""``/reset`` — wipe in-flight skill state from inside the TUI.

The pre-existing ``reyn chat --reset`` CLI flag prompts for confirmation
via ``input()`` BEFORE the TUI mounts, so users who realise mid-session
they want a reset have no in-app affordance — they have to ``Ctrl+D``,
restart with ``--reset``, answer the prompt at the bare terminal, then
get the TUI back. UX wave finding F14.

Two-step confirmation pattern (= no Textual ModalScreen / new widget
needed; the simplest path to a TUI-native flow):

  /reset            → prints the warning + asks user to type
                      ``/reset confirm`` to proceed.
  /reset confirm    → calls ``_reset_project_state(confirm=False)``
                      and reports the outcome.

The CLI flag stays — ``--cui`` mode (no TUI) and external-script
invocations still rely on it. This is a *parallel* affordance, not a
replacement.
"""
from __future__ import annotations

from reyn.slash import reply, reply_error, slash


def _format_currently_line(session: "object") -> str:
    """Build the 'Currently: N skills, M plans' context line.

    Reads from ``session.current_state_summary()`` when available.
    Returns an empty string when the session is not a full ChatSession
    (e.g. test stubs that don't expose the method).
    """
    summary_fn = getattr(session, "current_state_summary", None)
    if not callable(summary_fn):
        return ""
    try:
        s = summary_fn()
    except Exception:  # noqa: BLE001 — best-effort
        return ""
    n_skills = s.get("running_skills", 0)
    n_plans = s.get("running_plans", 0)
    skill_word = "skill" if n_skills == 1 else "skills"
    plan_word = "plan" if n_plans == 1 else "plans"
    return (
        f"Currently: {n_skills} {skill_word} running, "
        f"{n_plans} {plan_word}."
    )


@slash(
    "reset",
    summary="Reset in-flight skill state (snapshots + WAL; audit logs preserved)",
    usage="/reset confirm",
    see_also=("docs/guide/for-skill-authors/crash-recovery-and-resume.md",),
)
async def reset_cmd(session: "object", args: str) -> None:
    token = args.strip().lower()
    if token != "confirm":
        currently = _format_currently_line(session)
        preamble = f"{currently}\n" if currently else ""
        await reply(
            session,
            f"{preamble}"
            "⚠ This will delete all in-flight skill state "
            "(snapshots + WAL). Audit logs are preserved.\n"
            "Type `/reset confirm` to proceed, or anything else to abort.\n"
            "See docs/guide/for-skill-authors/crash-recovery-and-resume.md "
            "for what snapshots+WAL hold.",
        )
        return

    registry = getattr(session, "_registry", None)
    if registry is None:
        await reply_error(
            session,
            "registry not wired; /reset only works in `reyn chat`",
        )
        return
    project_root = getattr(registry, "_project_root", None)
    if project_root is None:
        await reply_error(
            session,
            "registry has no _project_root; /reset cannot locate state directory",
        )
        return

    # Lazy import so the slash registry doesn't pull the CLI module on every
    # session bootstrap (slash dispatch happens far more often than reset).
    from reyn.cli.commands.chat import _reset_project_state

    proceeded = _reset_project_state(project_root, confirm=False)
    if proceeded:
        await reply(
            session,
            "✓ State reset complete. Snapshots + WAL removed; audit logs "
            "preserved. Restart `reyn chat` for the changes to fully apply "
            "to the active session.",
        )
    else:
        # _reset_project_state only returns False when confirm=True and the
        # interactive prompt is declined — we passed confirm=False, so this
        # branch shouldn't normally hit. Guarded for defence in depth.
        await reply_error(session, "Reset did not proceed (unexpected).")
