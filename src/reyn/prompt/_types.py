"""Shared review-metadata type for the ``reyn.prompt`` package.

``PromptComponent`` is a frozen record wrapping ONE relocated SP string (or
string-producing helper's static piece) with the WHEN/WHERE/WHY + purpose-based
Japanese gist a reviewer needs, without duplicating that context as scattered
prose comments. Shaped in parallel with the sibling ``reyn.tools.descriptions``
package's ``ToolDescription`` record (same review-front-door pattern, applied to
SP text instead of tool ``description=`` strings) — deliberately NOT imported
from there: the two packages relocate different LLM-facing surfaces and must
stay independently reviewable/mergeable.

Only ``text`` is ever sent to the LLM. ``purpose`` and ``ja`` are review-only
metadata — they must never be concatenated into an assembled prompt.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class PromptComponent:
    """One reviewable unit of LLM-facing system-prompt text.

    Attributes:
        name: short identifier matching the module-level constant name
            (e.g. ``"TASK_COMPLETION_RULE"``) — lets a reviewer cross-reference
            this record against the raw constant in the same module.
        surfaced: WHEN/WHERE this text is rendered into the assembled system
            prompt (which builder, which slot/section, which gate).
        purpose: WHY this text exists — the rationale, in English.
        text: the EXACT LLM-facing string. This is the only field ever sent
            to the model.
        ja: purpose-based Japanese gist (what/when/why) — NOT a literal
            translation of ``text``. Review-only; never sent to the LLM.
    """

    name: str
    surfaced: str
    purpose: str
    text: str
    ja: str
