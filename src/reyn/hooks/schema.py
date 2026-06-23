"""reyn.hooks.schema — typed models for hook definitions (#1800 slice A).

Defines ``HookDef`` (a single hook entry from the ``hooks:`` config block)
and ``PushBlock`` (the inline inbox-push sub-schema).  Template strings are
stored **raw** — rendering is a later slice.

Hook-point identifiers are normalised lowercase; the allowed set is the
starter set agreed in #1800:

    turn_start   turn_end
    session_start  session_end
    skill_start  skill_end
    task_start   task_end
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Union

# ---------------------------------------------------------------------------
# Allowed hook-points (starter set — #1800 CONVERGED DESIGN)
# ---------------------------------------------------------------------------

ALLOWED_HOOK_POINTS: frozenset[str] = frozenset({
    "turn_start",
    "turn_end",
    "session_start",
    "session_end",
    "skill_start",
    "skill_end",
    "task_start",
    "task_end",
})


# ---------------------------------------------------------------------------
# Validation error
# ---------------------------------------------------------------------------


class HookConfigError(ValueError):
    """Raised when a ``hooks:`` entry fails structural validation.

    The message is decision-enabling: it names the offending entry index,
    the failing field, and a remediation hint so the operator can fix the
    config without reading source.
    """


# ---------------------------------------------------------------------------
# PushBlock — inbox-push sub-schema
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PushBlock:
    """Inbox-push directive for a hook definition.

    Stores Jinja2 templates as **raw strings** (rendering is slice B).

    Fields
    ------
    message:
        Jinja2 template string that renders to the message content to push
        into the session inbox.  Required.
    wake:
        Controls whether the pushed message triggers a new turn (``True``)
        or rides along with the next scheduled turn (``False``).  May be a
        plain bool or a Jinja2 template string that renders to a bool.
        Default: ``True`` (the push-and-wake / self-continuation path,
        matching the dominant use-case E from the design).
    push_when:
        Optional Jinja2 template string that renders to a bool.  When
        ``False`` the push is skipped entirely (conditional push). Default
        ``"true"`` (always push).
    session:
        Optional Jinja2 template string or static session identifier.
        When absent the runtime will default to the current session.
    """

    message: str
    wake: Union[bool, str] = True
    push_when: str = "true"
    session: str | None = None


# ---------------------------------------------------------------------------
# HookDef — the top-level hook entry
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class HookDef:
    """A single lifecycle hook definition.

    Exactly one of ``push`` or ``shell`` must be set (validated by the
    loader, not by the dataclass itself — the dataclass is a plain data
    container).

    Fields
    ------
    on:
        Hook-point name — one of ``ALLOWED_HOOK_POINTS``.
    name:
        Optional operator label for the hook (#1800 slice 6). Surfaced as the
        ``[hook:<name>]`` attribution prefix on a push. **Absent → the dispatcher
        defaults it to the hook-point** (``on``), preserving slice-5b behavior.
    push:
        Inbox-push hook block.  Mutually exclusive with ``shell``.
    shell:
        Shell command to run.  Stored raw; the runner is a later slice.
        Mutually exclusive with ``push``.
    matcher:
        Reserved optional filter string.  Not interpreted in this slice.
    """

    on: str
    name: str | None = field(default=None)
    push: PushBlock | None = field(default=None)
    shell: str | None = field(default=None)
    matcher: str | None = field(default=None)
