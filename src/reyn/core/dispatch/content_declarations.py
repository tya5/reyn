"""Per-tool "this field is conversation content" declarations (#4666 item ③b).

#4666's owner ruling created 3 content opt-ins — ``agent_delta_include_text``
(①), ``completed_response_include_text`` (②), ``user_input_include_text``
(③) — each governing a FIXED set of audit-event kinds whose payload shape
`LocalEventBackend` already knows at the write-side chokepoint. ``ask_user``
broke that: the SAME question/answer text ALSO reaches the audit log via
``tool_called.args["question"]`` / ``tool_returned.result["answer"]``
(``dispatch_tool``, #4970's own reyn-reviewer/architect finding) — a
generic, per-tool-args event whose shape `dispatch_tool` cannot know in
advance for every tool that will ever exist.

Architect's ruling (#4666, after an initial "4th knob" proposal was
rejected): don't add a 4th knob keyed by CARRIER (which event a field
rides) — the owner's 3 knobs are already keyed by CONTENT CLASS, and a
carrier-keyed 4th would split the vocabulary onto two incompatible axes.
Instead, the TOOL that owns a field declares what content class it is —
knowledge lives where it is known (a generic dispatcher does not know
which of an arbitrary tool's args is a user's own words; the tool that
defined that arg does). ``dispatch_tool`` only CONSULTS this registry; it
never hardcodes a tool name.

Today exactly one tool has a non-empty declaration: ``ask_user`` — its
``question`` argument is content the MODEL directed at the user (②'s
content class), its ``answer`` result field is content the USER typed
back (③'s content class). See ``reyn.tools.ask_user`` for the
``declare_content_fields`` call site.

⚠️ Known bound weakness (architect, #4666, kept verbatim on purpose): a
tool that shows the user free text and forgets to call
``declare_content_fields`` leaks that text through ``tool_called``/
``tool_returned`` silently — the bound test below (`declared_tools`)
only catches the set GROWING, never catches a tool that SHOULD have
declared and didn't. There is no structural fix for that half; it needs
a human noticing a new user-facing free-text tool at review time.

⚠️ Reconsideration trigger (architect, #4666, kept verbatim on purpose):
if a SECOND tool ever gets a non-empty declaration, reconsider inverting
this — "the dispatcher redacts by default, and a tool that WANTS its
content durably recorded declares that" — rather than the current
"opt-out per field" shape. One declared tool does not justify that
inversion; a second one is the trigger to re-open the question, not to
silently re-derive the same shape again.
"""
from __future__ import annotations

from typing import Literal, Mapping

#: Which of #4666's 3 content opt-ins governs a declared field:
#:   "assistant" -> ``audit_events.completed_response_include_text`` (②)
#:     — content the MODEL directed at the user.
#:   "user"      -> ``audit_events.user_input_include_text`` (③)
#:     — content the USER typed/chose.
#: (``agent_delta_include_text``, ①, has its own dedicated coalescing
#: write path in `LocalEventBackend` and is never reached through this
#: registry — a streamed reply chunk is not a tool call.)
ContentClass = Literal["assistant", "user"]

_TOOL_CONTENT_FIELDS: dict[str, dict[str, ContentClass]] = {}


def declare_content_fields(tool_name: str, fields: Mapping[str, ContentClass]) -> None:
    """Register *tool_name*'s field -> content-class mapping.

    Called once, at import time, by the tool's own module (mirrors this
    tree's existing ``register(kind, handler, ...)`` idiom elsewhere in
    ``core/op_runtime``) — never by `dispatch_tool` itself, which only
    reads this registry."""
    _TOOL_CONTENT_FIELDS[tool_name] = dict(fields)


def get_content_fields(tool_name: str) -> Mapping[str, ContentClass]:
    """Return *tool_name*'s declared field -> content-class mapping, or
    ``{}`` if it never declared any (the default for every tool that
    isn't ``ask_user`` today)."""
    return _TOOL_CONTENT_FIELDS.get(tool_name, {})


def declared_tools() -> frozenset[str]:
    """The set of tool names with a NON-EMPTY declaration — read by the
    bound test (`tests/core/test_4666_tool_content_declarations.py`) so
    a new declaration is a deliberate, reviewed addition, not a silent
    one (mirrors this tree's `_force_inline` AST-enumeration bound)."""
    return frozenset(_TOOL_CONTENT_FIELDS)
