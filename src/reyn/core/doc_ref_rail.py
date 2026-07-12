"""D5c error-rail: append a ``doc_ref`` pointer to a parse/validation error.

Proposal 0060 Addendum D, D5c. The reachability audit (D2) found a 5th
channel it hadn't checked: error messages. A parse/validation failure
returned to the model is the highest-value teaching moment — the re-prompt
loop already exists, so a pointer here (to the doc that fully specifies the
format the model just got wrong) teaches the fix instead of leaving the model
to hallucinate a retry.

Single-home content, multi-home pointers (D3 rule 2): this module does not
duplicate doc content, only appends a short "see <doc_ref>" suffix. Callers
are the exception classes whose messages reach the model unmodified today
(``PipelineParseError``, ``PresentBlueprintError``) — wiring the suffix into
``__init__`` covers every existing (and future) raise site in one place,
instead of hand-editing each ``raise`` call.
"""
from __future__ import annotations


def with_doc_pointer(message: str, doc_ref: str) -> str:
    """Append a ``(see <doc_ref> for the full spec)`` suffix to ``message``,
    unless it is already present (idempotent — avoids doubling the suffix if
    a caller re-wraps an already-annotated message)."""
    suffix = f" (see {doc_ref} for the full spec)"
    if suffix in message:
        return message
    return f"{message}{suffix}"
