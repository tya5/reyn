"""Tier 2: docs/concepts/runtime/delegation-policy.md's two floor tables
stay in sync with the code they describe (capability_profile.py's
``_FLOORED_DENY_CLASSES`` / ``DELEGATION_AUDIT_CLASSES``).

Named ahead of need, proposal 0067 P6 (#3978): the same doc/code pair
drifted apart THREE times in one night (P5 added a class member without
touching the doc; #4117 added ``run_prompt`` to the code floor without the
doc; a 4-commit rebase silently reverted one doc table back to 2 names
mid-merge, caught only by manual visual review) — architect's own
discriminator for "build a gate, don't just record a checklist item" is
whether the check is STRUCTURAL (parse a markdown table row, compare to a
frozenset — deterministic, near-zero false-positive rate for a stable table
shape) or SEMANTIC (requires judgment). This is structural: read the two
tables, extract each row's backtick-quoted tool list, and diff it against
the single source of truth every other test in this arc already trusts.

A markdown-format change that breaks the regex here should be read as "the
parser needs updating", not "the gate was wrong to exist" — same failure
mode this repo's own S3 deny-set equality gate already accepts for its own
class of structural check.

⚠️ This gate is structural only WHILE the table's shape (column order,
backtick-quoting convention) holds. If someone adds a column, reorders one,
or changes the quoting style, this file goes RED for a reason that is NOT
"the doc drifted from the code" — the repair in that case is THIS PARSER,
not the doc. A future reader who sees RED here should read the diff before
assuming the doc is the thing to fix.
"""
from __future__ import annotations

import re
from pathlib import Path

from reyn.security.permissions.capability_profile import (
    _FLOORED_DENY_CLASSES,
    DELEGATION_AUDIT_CLASSES,
)

_DOC_PATH = (
    Path(__file__).resolve().parents[2]
    / "docs" / "concepts" / "runtime" / "delegation-policy.md"
)

_TOOL_RE = re.compile(r"`([a-zA-Z_][a-zA-Z0-9_]*)`")


def _parse_class_tools_table(
    lines: "list[str]", *, header_prefix: str, tools_col_index: int,
) -> "dict[str, frozenset[str]]":
    """Parse a markdown table (class in column 0) into
    ``{class: frozenset(tools)}``, reading the tool list from
    ``tools_col_index`` explicitly — a free-text rationale/prose column may
    ALSO mention a tool name in backticks (e.g. "the same way
    `delegate_to_agent` does"), so this never scans the whole line or infers
    the column from its shape."""
    start = next(i for i, l in enumerate(lines) if l.startswith(header_prefix))
    result: "dict[str, frozenset[str]]" = {}
    for line in lines[start + 2:]:  # skip the header + the |---|---| separator
        if not line.startswith("|"):
            break  # table ended
        cols = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cols) <= tools_col_index or not cols[0].startswith("`"):
            continue  # header/other non-data row
        cls = cols[0].strip("`")
        tools = frozenset(_TOOL_RE.findall(cols[tools_col_index]))
        result[cls] = tools
    return result


def _parse_severity_table(lines: "list[str]", *, header_prefix: str) -> "dict[str, str]":
    start = next(i for i, l in enumerate(lines) if l.startswith(header_prefix))
    result: "dict[str, str]" = {}
    for line in lines[start + 2:]:
        if not line.startswith("|"):
            break
        cols = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cols) < 3:
            continue
        cls = cols[0].strip("`")
        severity = cols[1].strip()
        if not cls or severity not in ("HIGH", "MED", "INFO"):
            continue
        result[cls] = severity
    return result


def test_permission_floor_table_matches_floored_deny_classes():
    """Tier 2: delegation-policy.md's "Built-in deny set" table (the
    permission-floor table) names, per class, exactly the tools
    ``_FLOORED_DENY_CLASSES`` denies — no more, no fewer, no drifted name."""
    lines = _DOC_PATH.read_text(encoding="utf-8").splitlines()
    doc_table = _parse_class_tools_table(
        lines, header_prefix="| Class | Denied tools", tools_col_index=1,
    )
    assert doc_table == dict(_FLOORED_DENY_CLASSES), (
        f"delegation-policy.md's permission-floor table has drifted from "
        f"_FLOORED_DENY_CLASSES.\ndoc: {doc_table}\ncode: {dict(_FLOORED_DENY_CLASSES)}"
    )


def test_audit_class_table_matches_delegation_audit_classes():
    """Tier 2: delegation-policy.md's "Audit classes" table names, per
    class, exactly the tools + severity ``DELEGATION_AUDIT_CLASSES`` uses
    (including the documented audit-only ``destructive-fs`` exception)."""
    lines = _DOC_PATH.read_text(encoding="utf-8").splitlines()
    doc_tools = _parse_class_tools_table(
        lines, header_prefix="| Class | Severity", tools_col_index=2,
    )
    doc_severity = _parse_severity_table(lines, header_prefix="| Class | Severity")

    code_tools = {cls: tools for cls, (_sev, tools) in DELEGATION_AUDIT_CLASSES.items()}
    code_severity = {cls: sev for cls, (sev, _tools) in DELEGATION_AUDIT_CLASSES.items()}

    assert doc_tools == code_tools, (
        f"delegation-policy.md's audit-class table tool lists have drifted "
        f"from DELEGATION_AUDIT_CLASSES.\ndoc: {doc_tools}\ncode: {code_tools}"
    )
    assert doc_severity == code_severity, (
        f"delegation-policy.md's audit-class table severities have drifted "
        f"from DELEGATION_AUDIT_CLASSES.\ndoc: {doc_severity}\ncode: {code_severity}"
    )
