"""Name reyn's own process so `ps` and Activity Monitor can identify it.

WHY THIS EXISTS
    On 2026-08-09 the operator's machine needed two reboots after a
    ``python3.12`` process reached ~29 GB. Nothing on the machine could say
    whether that process was reyn, a test run, a measurement script, or an
    unrelated tool: every one of them reports the interpreter's name. The
    interpreter's name is the one part of the answer that is never in doubt
    and never useful.

    Naming the process converts "some python is eating the machine" into
    "reyn:chat is eating the machine" — or, just as valuable, into "this is
    not reyn". Charter lens 8 (Product Think: predictable, legible to the
    operator) is the one this serves.

WHAT GOES IN THE NAME
    The subcommand, and nothing else. A process title is world-readable
    through ``ps`` — every user on the host sees it. Workspace paths, session
    ids, prompts and file arguments are all things reyn knows at this point
    and none of them belong in a string the whole machine can read. "Which
    reyn is this" is answered by the subcommand; the rest is a leak with a
    diagnostic excuse.

WHEN IT DOES NOTHING
    ``setproctitle`` is a compiled extension. If it is missing this module is
    a no-op and reyn runs exactly as before — a diagnostic aid that can stop
    a program from starting is worse than the diagnosis it offers.
"""
from __future__ import annotations

PREFIX = "reyn"


def format_title(subcommand: str | None) -> str:
    """The title reyn would set for ``subcommand``.

    Split out from :func:`set_process_title` so the naming rule can be
    asserted without a subprocess: what the string looks like and whether the
    OS accepted it are two different claims, and only the second one needs a
    real process to check.
    """
    if not subcommand:
        return PREFIX
    return f"{PREFIX}:{subcommand}"


def set_process_title(subcommand: str | None) -> bool:
    """Set this process's visible name. Returns whether it was actually set.

    The return value is the honest one: ``False`` means the machine still
    shows ``python3.12``, which is exactly the state this module exists to
    remove, so a caller that wants to warn about it can. Nothing in reyn
    treats a ``False`` as an error — see the module docstring.
    """
    try:
        import setproctitle  # noqa: PLC0415 — optional, and paid only on the CLI path
    except ImportError:
        return False
    setproctitle.setproctitle(format_title(subcommand))
    return True
