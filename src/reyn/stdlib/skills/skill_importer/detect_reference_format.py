"""Deterministic preprocessor for skill_importer.convert — detect
reference-format source skills.

Called as a ``type: python`` preprocessor step. Signature contract:
  ``detect(artifact: dict) -> dict``

Input (from ``artifact["data"]``):
  ``source_url`` — directly-fetchable URL of the skill's source markdown
    (= selected_candidate.data.source_url).

Output (placed at ``data._reference_format_check``):
  ``is_reference_format`` — bool. True when the source description
    matches Anthropic's canonical trigger-enumeration pattern, the
    deterministic signal that the source is a reference manual and
    skill_importer MUST emit a single-phase graph (= PR #583
    discipline, lifted from 20% prompt-only honor rate to 100% machine
    enforcement when the marker is present).
  ``signal`` — short string naming which pattern matched
    (``trigger_list``, ``includes_phrase``, ``mention_clause``, or
    ``none``). For diagnostics in the LLM's reasoning surface.
  ``description`` — the parsed source ``description`` field, stripped
    + single-line-joined, capped at 600 chars. Empty when fetch /
    parse fails. The convert phase can use this directly for the
    imported skill.md's ``description:`` field, avoiding a separate
    LLM pass to extract it.
  ``fetch_status`` — ``ok``, ``http_<code>``, or ``error``. Empty
    description + ``fetch_status=ok`` means the source had no
    frontmatter description, NOT that fetching failed.

Failure handling: any exception → ``is_reference_format=false`` +
``signal=none`` + ``fetch_status=error``. The convert phase falls
back to its existing LLM-driven decomposition discipline (= PR #583)
in that case.

I/O route: ``reyn.api.unsafe.http.get`` (= urllib, no extra deps),
same as ``mcp_search`` / ``skill_search`` preprocessors.
"""
from __future__ import annotations

import re

from reyn.api.unsafe.http import get as http_get

_USER_AGENT = "reyn/1.0"

# Patterns that mark reference-format skills. The match logic is two-stage:
# (1) find a "trigger anchor" phrase like "This includes", and
# (2) confirm the same sentence has ≥ 2 list connectors (= commas or "or").
# This catches the Anthropic enumeration shape (= "This includes reading,
# merging, splitting, ..., and OCR") without false-positives on a single
# "This includes X" mention.
_TRIGGER_ANCHORS: tuple[tuple[str, "re.Pattern[str]"], ...] = (
    # "This includes <enumeration>" — the hallmark Anthropic pattern.
    # PDF, pptx use this.
    (
        "includes_phrase",
        re.compile(r"\bThis includes\b:?([^.]+)", re.IGNORECASE),
    ),
    # "This means <enumeration>" — variant. xlsx uses this.
    (
        "means_phrase",
        re.compile(r"\bThis means\b\s*[^.]*?([:,][^.]+)", re.IGNORECASE),
    ),
    # "Triggers include: <enumeration>" / "Triggers: <enum>" — docx variant.
    (
        "triggers_include",
        re.compile(r"\bTriggers?\b\s+include[s]?\b:?([^.]+)", re.IGNORECASE),
    ),
    # "TRIGGER when: <enumeration>" — claude-api uses this caps form.
    (
        "trigger_when",
        re.compile(r"\bTRIGGER\b\s+when\b:?([^.]+)", re.IGNORECASE),
    ),
    # "When the user wants to <op>, <op>, or <op>" / "asks to" / "mentions"
    (
        "trigger_list",
        re.compile(
            r"\bWhen the user (?:wants to|asks to|mentions)\b([^.]+)",
            re.IGNORECASE,
        ),
    ),
    # "Also use when <op>, <op>, or <op>" — secondary enumeration that
    # often follows the primary description.
    (
        "also_use_when",
        re.compile(r"\bAlso use when\b([^.]+)", re.IGNORECASE),
    ),
    # "Use whenever <op>, <op>, or <op>" — Reyn pushy-description pattern
    # (= PR #564), which user-built skills also follow.
    (
        "use_whenever",
        re.compile(r"\bUse when(?:ever)?\b\s+the user\b([^.]+)", re.IGNORECASE),
    ),
    # "If the user mentions <a> ... or <b>" — closing-sentence variant.
    (
        "mention_clause",
        re.compile(r"\bIf the user mentions\b([^.]+)", re.IGNORECASE),
    ),
)


def _count_list_connectors(s: str) -> int:
    """Count commas + 'or' word-boundaries in ``s`` (= enumeration depth)."""
    commas = s.count(",")
    ors = len(re.findall(r"\bor\b", s, re.IGNORECASE))
    return commas + ors


_FRONTMATTER_DESC_RE = re.compile(
    r"^description:\s*(.+?)(?:\n[a-zA-Z_-]+:|\n---)",
    re.DOTALL | re.MULTILINE,
)


# Body-side stage markers — Anthropic workflow-format skills use these
# section headings to delineate sequential stages. When the body has 2+
# of these, classify as workflow regardless of how trigger-enumerated
# the description sounds (= avoids the doc-coauthoring false-positive
# where description enumerates triggers but body is a sequential workflow).
_STAGE_MARKER_RE = re.compile(
    r"^#{1,3}\s*(?:Step|Stage|Phase)\s+\d+\b",
    re.IGNORECASE | re.MULTILINE,
)


def _count_stage_markers(body: str) -> int:
    """Count ``## Step N`` / ``## Stage N`` / ``## Phase N`` headings."""
    return len(_STAGE_MARKER_RE.findall(body))


def _parse_description(body: str) -> str:
    """Extract the ``description`` field from a SKILL.md's YAML frontmatter.

    Returns the raw value with newlines collapsed to single spaces +
    leading/trailing whitespace stripped. Empty string when the body
    has no frontmatter or no description field. Cap at 600 chars to
    keep the artifact small.
    """
    if not body.startswith("---"):
        return ""
    end = body.find("---", 3)
    if end == -1:
        return ""
    fm = body[3:end] + "\n---"
    m = _FRONTMATTER_DESC_RE.search(fm)
    if not m:
        return ""
    raw = m.group(1)
    # Block-scalar form (``description: |\n  line1\n  line2``) lands
    # here with the ``|`` as the first non-whitespace char. Strip
    # the marker.
    raw = raw.lstrip("|").lstrip(">")
    return " ".join(raw.split())[:600]


def _match_signal(description: str) -> str:
    """Return the first anchor name whose enumeration depth ≥ 2, else
    ``"none"``.

    Two-stage match: each anchor's regex captures the trailing
    text up to the next period; we then count comma + "or" connectors
    in that captured span. Depth ≥ 2 means the source is enumerating
    a list of triggers / operations (= reference-format).
    """
    if not description:
        return "none"
    for name, rx in _TRIGGER_ANCHORS:
        m = rx.search(description)
        if m and _count_list_connectors(m.group(1)) >= 2:
            return name
    return "none"


def detect(artifact: dict) -> dict:
    """Preprocessor entry — fetch the source, check trigger-enumeration.

    Defensive: any failure returns
    ``{is_reference_format: false, signal: "none", description: "",
      fetch_status: "error"}`` so the LLM falls through to PR #583's
    prompt-only decomposition discipline.
    """
    data = artifact.get("data") or {}
    source_url = data.get("source_url") or ""
    if not isinstance(source_url, str) or not source_url.strip():
        return {
            "is_reference_format": False,
            "signal": "none",
            "description": "",
            "fetch_status": "error",
        }

    try:
        resp = http_get(source_url, headers={"User-Agent": _USER_AGENT})
    except Exception:
        return {
            "is_reference_format": False,
            "signal": "none",
            "description": "",
            "fetch_status": "error",
        }

    status = int(resp.get("status") or 0)
    if status >= 400:
        return {
            "is_reference_format": False,
            "signal": "none",
            "description": "",
            "fetch_status": f"http_{status}",
        }

    body = resp.get("body") or ""
    if not isinstance(body, str):
        try:
            body = body.decode("utf-8", errors="replace")
        except Exception:
            body = ""

    description = _parse_description(body)
    signal = _match_signal(description)
    # Anti-signal: workflow-format body. If ≥ 2 explicit stage markers
    # in the body (= "## Step N" / "## Stage N" / "## Phase N"), the
    # source IS a sequential workflow even if its description happens
    # to enumerate triggers. Avoid the false-positive where a workflow
    # skill's user-facing trigger list gets it mis-classified as
    # reference-format. The discriminator runs AFTER the trigger-anchor
    # match so we can report both signals back for diagnostics.
    stage_marker_count = _count_stage_markers(body)
    is_workflow_body = stage_marker_count >= 2
    is_reference_format = signal != "none" and not is_workflow_body
    return {
        "is_reference_format": is_reference_format,
        "signal": signal,
        "stage_marker_count": stage_marker_count,
        "description": description,
        "fetch_status": "ok",
    }
