"""Strict / canonical JSON helpers."""

from __future__ import annotations

import json as _json
from typing import Any


def _no_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    seen: set[str] = set()
    out: dict[str, Any] = {}
    for k, v in pairs:
        if k in seen:
            raise ValueError(f"duplicate key in JSON object: {k!r}")
        seen.add(k)
        out[k] = v
    return out


def loads_strict(s: str) -> Any:
    """Parse ``s`` as JSON, rejecting objects with duplicate keys.

    Standard ``json.loads`` silently overwrites duplicate keys; this
    wrapper raises ``ValueError`` instead.
    """
    return _json.loads(s, object_pairs_hook=_no_duplicate_keys)


def dumps_canonical(d: Any) -> str:
    """Dump ``d`` in canonical form (= ``sort_keys=True``, non-ASCII kept).

    Suitable for content addressing — two equal objects produce the
    same byte sequence.
    """
    return _json.dumps(d, sort_keys=True, ensure_ascii=False)
