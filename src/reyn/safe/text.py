"""Text helpers: named-group regex extraction and safe templating."""

from __future__ import annotations

import re as _re


def regex_findall_named(pattern: str, text: str) -> list[dict[str, str]]:
    """Return a list of named-group dicts for every match of ``pattern``.

    Each entry is the ``match.groupdict()`` of one match. Groups that
    did not participate in the match map to an empty string. Matches
    without any named groups produce empty dicts.
    """
    regex = _re.compile(pattern)
    out: list[dict[str, str]] = []
    for m in regex.finditer(text):
        gd = m.groupdict()
        out.append({k: (v if v is not None else "") for k, v in gd.items()})
    return out


class _SafeDict(dict):
    """Dict that returns ``{key}`` literally for missing keys."""

    def __missing__(self, key: str) -> str:  # type: ignore[override]
        return "{" + key + "}"


def template_render_safe(template: str, ctx: dict) -> str:
    """Render ``template`` with ``str.format_map`` over a safe dict.

    Missing keys render literally as ``{key}`` rather than raising
    ``KeyError``. No attribute access or method calls are evaluated
    via this helper since callers only provide a plain ``dict``.
    """
    return template.format_map(_SafeDict(ctx))
