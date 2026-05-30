"""Lenient JSON parsing for LLM output — strict -> repair -> raw_decode tiers."""
from __future__ import annotations

import json
import re
from typing import Any, Callable

_TRAILING_COMMA_RE = re.compile(r",(\s*[}\]])")


def _repair_trailing_commas(text: str) -> str:
    """Remove trailing commas — the most common LLM JSON mistake."""
    return _TRAILING_COMMA_RE.sub(r"\1", text)


def loads_lenient(
    text: str,
    *,
    on_raw_decode: Callable[[int, str], None] | None = None,
) -> Any:
    """Parse LLM-emitted JSON with escalating leniency.

    Tier 1: strict json.loads.
    Tier 2: trailing-comma repair, then json.loads.
    Tier 3: json.JSONDecoder().raw_decode() — extracts the leading
            complete JSON value and discards trailing garbage (the
            13977 failure: a valid object followed by extra data).

    ``on_raw_decode(discarded_len, head)`` is invoked when Tier 3 fires
    (= observability; never silent-recover). ``discarded_len`` is the
    byte length of the discarded trailing portion; ``head`` is the first
    ~80 chars of the discarded portion for log context.

    Raises json.JSONDecodeError when even Tier 3 cannot extract a
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

    # Tier 3: raw_decode the leading value, discard trailing garbage.
    # raw_decode requires the value to start at index 0, so strip leading
    # whitespace (json.loads already handles this for tiers 1 and 2).
    stripped = text.lstrip()
    obj, end = json.JSONDecoder().raw_decode(stripped)
    # raw_decode raises JSONDecodeError itself if the leading value is
    # malformed — that propagates, preserving the "genuinely malformed" contract.
    discarded = stripped[end:]
    if on_raw_decode is not None and discarded.strip():
        on_raw_decode(len(discarded), discarded[:80])
    return obj
