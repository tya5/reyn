"""Phase preprocessor (#1209 PR-B): regex-escape each edit's ``anchor``.

The plan emits ``anchor`` = a short verbatim snippet from the current file at the
edit site (a grep landmark). The apply preprocessor then runs a deterministic
``grep`` to fetch that region into context BEFORE the model edits, so the model
never edits a file it cannot see (the apply-starvation root cause, #1209).

``grep`` compiles its pattern as a regex (``op_runtime/file.py:_execute_grep``),
so a verbatim snippet containing regex-special chars — parens, dots, brackets,
``*``/``+``/``|``, ubiquitous in source — must be escaped to match literally.
This step adds an ``anchor_re`` field per edit; the iterate step greps with it.

Pure data transform (no file access — runs in the sandboxed python-step).
Deterministic, P5-correct (the OS runs it before the apply LLM; never
LLM-mutated). ``args_from`` resolves ``_iter.item.anchor_re`` for the grep op.
"""
from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any


def escape_anchors(data: Mapping[str, Any]) -> list[dict]:
    """Return ``data['edits']`` with a regex-escaped ``anchor_re`` per edit.

    Edits without an ``anchor`` get the never-match sentinel ``(?!)`` (NOT an
    empty string: an empty regex matches every line via ``re.search("", line)``,
    which would wrongly land in the multi-match path — see #1214 review). The
    sentinel makes the iterate grep yield zero matches → the apply instructions
    treat it as not-locatable and report rather than blind-edit. Non-dict entries
    are skipped.

    The python step receives the FULL artifact: the edit plan lives at
    ``data["data"]["edits"]`` (inner dict), with a flat ``data["edits"]``
    fallback for unit tests that inject the inner data directly (mirrors
    ``parse_test_targets._extract_test_patch``). ``into: data.edits`` writes the
    returned list back to the same inner location.
    """
    inner = data.get("data") if isinstance(data.get("data"), dict) else data
    edits = inner.get("edits") or []
    out: list[dict] = []
    for edit in edits:
        if not isinstance(edit, dict):
            continue
        anchor = edit.get("anchor") or ""
        # Empty/missing anchor → never-match sentinel (NOT "" which matches every
        # line). `(?!)` is a valid regex that never matches → grep count 0.
        out.append({**edit, "anchor_re": re.escape(anchor) if anchor else "(?!)"})
    return out
