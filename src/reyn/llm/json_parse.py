"""Lenient JSON parsing for LLM output — strict -> repair -> raw_decode tiers."""
from __future__ import annotations

import json
import re
from typing import Any, Callable

_TRAILING_COMMA_RE = re.compile(r",(\s*[}\]])")


def _repair_trailing_commas(text: str) -> str:
    """Remove trailing commas — the most common LLM JSON mistake."""
    return _TRAILING_COMMA_RE.sub(r"\1", text)


# Valid JSON string escape characters following a backslash (RFC 8259):
# \" \\ \/ \b \f \n \r \t \uXXXX. Anything else is an invalid escape.
_VALID_ESCAPE_CHARS = '"\\/bfnrtu'


def _escape_invalid_backslashes(text: str) -> str:
    """Escape lone backslashes inside JSON strings that aren't valid escapes.

    LLMs frequently emit a bare ``\\`` inside a free-text string value — a regex
    (``\\d``), a Windows path (``C:\\Users``), a LaTeX/diff snippet — which is an
    invalid JSON escape and makes ``json.loads`` reject the whole (otherwise
    valid) object. This walks the text tracking in-string state and consumes
    escapes **pairwise**: a valid escape (``\\\\``, ``\\n``, ``\\uXXXX`` …) is
    kept as-is, a lone ``\\`` before any other char is doubled to ``\\\\``.

    Pairwise consumption is what makes an already-valid ``\\\\d`` (escaped
    backslash + ``d``) survive unchanged — a naive regex that doubles every
    "backslash not followed by an escape char" would corrupt it. ``\\uXXXX``
    with non-hex digits is left untouched (rare; out of scope — it still raises).
    """
    out: list[str] = []
    i = 0
    n = len(text)
    in_string = False
    while i < n:
        c = text[i]
        if not in_string:
            if c == '"':
                in_string = True
            out.append(c)
            i += 1
        elif c == '"':
            in_string = False
            out.append(c)
            i += 1
        elif c == "\\":
            nxt = text[i + 1] if i + 1 < n else ""
            if nxt in _VALID_ESCAPE_CHARS:
                # Valid escape — keep the pair intact (this preserves \\, \n, …).
                out.append(c)
                out.append(nxt)
                i += 2
            else:
                # Lone/invalid backslash — escape it so the string stays valid.
                out.append("\\\\")
                i += 1
        else:
            out.append(c)
            i += 1
    return "".join(out)


def loads_lenient(
    text: str,
    *,
    on_raw_decode: Callable[[int, str], None] | None = None,
) -> Any:
    """Parse LLM-emitted JSON with escalating leniency.

    Tier 1: strict json.loads.
    Tier 2: trailing-comma repair, then json.loads.
    Tier 3: invalid-backslash-escape repair (+ trailing-comma), then
            json.loads (the 13453 failure: a bare ``\\`` in a free-text
            value — regex / path / diff snippet — breaking the escape).
    Tier 4: json.JSONDecoder().raw_decode() on the repaired text —
            extracts the leading complete JSON value and discards trailing
            garbage (the 13977 failure: a valid object followed by extra data).

    ``on_raw_decode(discarded_len, head)`` is invoked when Tier 4 fires
    (= observability; never silent-recover). ``discarded_len`` is the
    byte length of the discarded trailing portion; ``head`` is the first
    ~80 chars of the discarded portion for log context.

    Raises json.JSONDecodeError when even Tier 4 cannot extract a
    leading value (= genuinely malformed from the first char).
    """
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    try:
        return json.loads(_repair_trailing_commas(text))
    except json.JSONDecodeError:
        pass

    # Tier 3: escape lone backslashes in strings (+ trailing-comma), then parse.
    repaired = _escape_invalid_backslashes(_repair_trailing_commas(text))
    try:
        return json.loads(repaired)
    except json.JSONDecodeError:
        pass

    # Tier 4: raw_decode the leading value of the repaired text, discard trailing
    # garbage. raw_decode requires the value to start at index 0, so strip leading
    # whitespace (json.loads already handles this for tiers 1–3).
    stripped = repaired.lstrip()
    obj, end = json.JSONDecoder().raw_decode(stripped)
    # raw_decode raises JSONDecodeError itself if the leading value is
    # malformed — that propagates, preserving the "genuinely malformed" contract.
    discarded = stripped[end:]
    if on_raw_decode is not None and discarded.strip():
        on_raw_decode(len(discarded), discarded[:80])
    return obj
