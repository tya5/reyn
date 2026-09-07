"""#5907 ②: the typed outcome of one client→server control POST.

A control request (a turn submit, an intervention answer, a cancel, a
session switch …) can end three ways, and a client must say which:

- ``delivered`` — the server accepted it (a 2xx); ``payload`` is the body.
- ``refused`` — the server answered and said no (≥300); ``reason`` is what
  it said, verbatim where there was a body.
- ``not_delivered`` — the server never answered: the control read timeout
  (#5894 ①-1) elapsed, or the request itself failed; ``cause`` names the
  exception class. **The request may or may not have reached the server**
  — the one thing a non-idempotent request's operator most needs to hear.

Before this, ``post_control`` collapsed the last two into ``None`` and
every handler rendered one line for both (architect, #5907: a falsy
carrying two facts is the fail-open class; the fix is a type, not a
check). Handlers keep their ``if not ok:`` — the transport records the
last outcome (``ClientTransport.last_control_outcome``) and the shared
renderers (``reply_error`` / ``dispatch._display``) append the outcome's
own wording, so 27 handlers say the right thing without being edited.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

Kind = Literal["delivered", "refused", "not_delivered"]


@dataclass(frozen=True)
class ControlOutcome:
    kind: Kind
    payload: "dict | None" = None
    status: "int | None" = None
    reason: "str | None" = None
    cause: "str | None" = None
    timeout_s: "float | None" = None

    def __bool__(self) -> bool:
        # Truthy iff delivered — so an ``if not outcome:`` reads as before.
        return self.kind == "delivered"

    @classmethod
    def delivered(cls, payload: dict) -> "ControlOutcome":
        return cls(kind="delivered", payload=payload)

    @classmethod
    def refused(cls, status: int, reason: "str | None") -> "ControlOutcome":
        return cls(kind="refused", status=status, reason=reason)

    @classmethod
    def not_delivered(cls, cause: str, timeout_s: "float | None") -> "ControlOutcome":
        return cls(kind="not_delivered", cause=cause, timeout_s=timeout_s)


def describe_control_failure(outcome: "ControlOutcome | None") -> "str | None":
    """The outcome's own wording for a failure line, or ``None`` when there
    is nothing to add (delivered, or no typed outcome recorded — an
    untyped sender, the local transport). One function, so a refusal and a
    non-delivery can never read the same on any surface."""
    if outcome is None or outcome.kind == "delivered":
        return None
    if outcome.kind == "refused":
        detail = f" ({outcome.status}: {outcome.reason})" if outcome.reason else f" ({outcome.status})"
        return f"the server refused it{detail}"
    within = f" within {outcome.timeout_s:g}s" if outcome.timeout_s is not None else ""
    cause = f" [{outcome.cause}]" if outcome.cause else ""
    return (
        f"the server did not respond{within}{cause} — the request may not have "
        "reached it"
    )


__all__ = ["ControlOutcome", "Kind", "describe_control_failure"]
