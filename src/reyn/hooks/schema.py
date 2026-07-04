"""reyn.hooks.schema — typed models for hook definitions (#1800 slice A).

Defines ``HookDef`` (a single hook entry from the ``hooks:`` config block)
and ``PushBlock`` (the inline inbox-push sub-schema).  Template strings are
stored **raw** — rendering is a later slice.

Hook-point identifiers are normalised lowercase; the allowed set is the
starter set agreed in #1800 (skill_start/skill_end removed — never dispatched):

    turn_start   turn_end
    session_start  session_end
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

    Exactly one of ``template_push`` / ``shell_exec`` / ``shell_push`` must be set
    (validated by the loader, not by the dataclass itself — the dataclass is a
    plain data container). The three consistent ``<source>_<action>`` keywords
    (#2069 converged design):

    Fields
    ------
    on:
        Hook-point name — one of ``ALLOWED_HOOK_POINTS``.
    name:
        Optional operator label for the hook (#1800 slice 6). Surfaced as the
        ``[hook:<name>]`` attribution prefix on a push. **Absent → the dispatcher
        defaults it to the hook-point** (``on``), preserving slice-5b behavior.
    template_push:
        Declarative inbox-push block from config Jinja2 templates (C/E). The
        push directive is computed from the template against event/context.
        Mutually exclusive with ``shell_exec`` / ``shell_push``.
    shell_exec:
        Shell command run as a pure side-effect — **output IGNORED**. Mutually
        exclusive with ``template_push`` / ``shell_push``.
    shell_push:
        Shell command whose **stdout is a JSON push-directive**
        (``{push_when, wake, message, session?}``, #2069) → pushed via the same
        C/E dispatch path as ``template_push``. Mutually exclusive with the
        other two.
    matcher:
        Reserved optional filter string.  Not interpreted in this slice.
    """

    on: str
    name: str | None = field(default=None)
    template_push: PushBlock | None = field(default=None)
    shell_exec: str | None = field(default=None)
    shell_push: str | None = field(default=None)
    matcher: str | None = field(default=None)
