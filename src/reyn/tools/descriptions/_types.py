"""ToolDescription — the reviewable record for one tool's LLM-facing text.

A ToolDescription pairs the exact ``text`` sent to the LLM (byte-identical to
what the tool's ToolDefinition.description carries) with review-aid metadata
that is never sent to the LLM: ``surfaced`` (WHEN/WHERE the description is
exposed — which gates / scheme), ``purpose`` (WHY the tool exists, one line),
and ``ja`` (a purpose-based Japanese description of what/when/why the tool is
for — NOT a literal translation of ``text`` — for a reviewer who reads
Japanese faster than English prose).

Fields ``surfaced`` / ``purpose`` / ``ja`` are documentation metadata only;
nothing in the runtime tool-dispatch path reads them. Only ``text`` is
LLM-facing.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ToolDescription:
    """A single tool's LLM-facing description plus review-aid metadata.

    Attributes:
        tool_name: The canonical ``ToolDefinition.name`` this description
            belongs to (e.g. ``"semantic_search"``). Used by the liveness
            check to confirm every entry maps to a real registered tool.
        surfaced: WHEN/WHERE this description is surfaced — which gates
            (router/phase) and which scheme(s) expose it to the LLM.
        purpose: WHY this tool exists, in one line.
        text: The EXACT string sent to the LLM as
            ``ToolDefinition.description``. Must be byte-identical to the
            pre-migration string — this is the reviewable artifact.
        ja: Purpose-based Japanese description (what/when/why) for review
            — NOT a literal translation of ``text``. Never sent to the LLM.
    """

    tool_name: str
    surfaced: str
    purpose: str
    text: str
    ja: str


@dataclass(frozen=True)
class ParamDescription:
    """One JSON-schema parameter's LLM-facing ``description`` plus a Japanese gloss.

    Phase 4 of the tool-description package refactor extends the reviewable
    package below tool-LEVEL text to PARAM-level text: the per-field
    ``"description": "..."`` strings inside a ``ToolDefinition.parameters``
    JSON-schema (``properties.<field>.description``). Not every tool has
    param-level descriptions — many params are self-evident from their name
    + type alone, so a bucket's ``PARAMS`` dict only carries entries for
    fields that actually declare one in the origin schema.

    Attributes:
        text: The EXACT string sent to the LLM as
            ``parameters["properties"][field]["description"]``. Must be
            byte-identical to the pre-migration string.
        ja: Purpose-based Japanese gloss of what the field is / why it
            matters — NOT a literal translation of ``text`` — for a
            reviewer who reads Japanese faster than English prose. Never
            sent to the LLM.
    """

    text: str
    ja: str
