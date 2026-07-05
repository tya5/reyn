"""Shared choice ids + factories for permission-style UserInterventions.

Both `permissions.py` (producer) and `interfaces/repl/renderer.py` (consumer) reference
the same id strings — keep them in one module so a typo or rename surfaces
in a single place.
"""
from __future__ import annotations

from reyn.user_intervention import InterventionChoice

# Choice ids
YES = "yes"           # one-shot allow (session-only)
ALWAYS = "always"     # allow + persist to .reyn/approvals.yaml
NO = "no"             # one-shot deny
NEVER = "never"       # deny + persist
JUST_PATH = "just_path"  # file approval: persist for this exact path
RECURSIVE = "recursive"  # file approval: persist for parent/declared dir
ACCEPT = "accept"     # #2597 slice ③: MCP elicitation gate — proceed
DECLINE = "decline"   # #2597 slice ③: MCP elicitation gate — refuse to answer


def generic_yn_choices() -> list[InterventionChoice]:
    """Standard `[y]es / [A]lways / [n]o / [N]ever` set used by `_prompt`."""
    return [
        InterventionChoice(id=YES, label="[y]es", hotkey="y"),
        InterventionChoice(id=ALWAYS, label="[A]lways", hotkey="A"),
        InterventionChoice(id=NO, label="[n]o", hotkey="n"),
        InterventionChoice(id=NEVER, label="[N]ever", hotkey="N"),
    ]


def shell_hook_choices() -> list[InterventionChoice]:
    """`[y]es / [A]lways / [n]o` for shell-hook consent (#2095).

    No `NEVER`: the shell-hook allowlist persists approvals only (there is no
    persistent-deny entry), so `ALWAYS` records to the allowlist and a plain
    `[n]o` skips this run without persisting.
    """
    return [
        InterventionChoice(id=YES, label="[y]es", hotkey="y"),
        InterventionChoice(id=ALWAYS, label="[A]lways", hotkey="A"),
        InterventionChoice(id=NO, label="[n]o", hotkey="n"),
    ]


def file_access_choices(recursive_label: str) -> list[InterventionChoice]:
    """`[y]es / [j]ust this path / [r]ecursive / [N]o` for file access approval.

    `recursive_label` is the directory the [r] option will persist (used as
    part of the visible label only — the resolver derives the actual path).
    """
    return [
        InterventionChoice(id=YES, label="[y]es", hotkey="y"),
        InterventionChoice(id=JUST_PATH, label="[j]ust this path always", hotkey="j"),
        InterventionChoice(
            id=RECURSIVE,
            label=f"[r]ecursive under {recursive_label!r} always",
            hotkey="r",
        ),
        InterventionChoice(id=NO, label="[N]o", hotkey="N"),
    ]


def elicitation_gate_choices() -> list[InterventionChoice]:
    """`[y]es / [N]o` for an MCP elicitation accept/decline gate (#2597 slice ③).

    Distinct ids (``ACCEPT``/``DECLINE``) from the generic ``YES``/``NO``
    permission-grant vocabulary: an elicitation gate answers "will you engage
    with this server's question at all", not "grant this permission" — a
    separate MCP protocol action (``action: accept | decline | cancel``), not
    a reyn permission decision. No ``ALWAYS``/``NEVER`` — an elicitation gate
    decision is never persisted; every elicitation is asked fresh.
    """
    return [
        InterventionChoice(id=ACCEPT, label="[y]es", hotkey="y"),
        InterventionChoice(id=DECLINE, label="[N]o", hotkey="N"),
    ]


__all__ = [
    "ACCEPT",
    "ALWAYS",
    "DECLINE",
    "JUST_PATH",
    "NEVER",
    "NO",
    "RECURSIVE",
    "YES",
    "elicitation_gate_choices",
    "file_access_choices",
    "generic_yn_choices",
    "shell_hook_choices",
]
