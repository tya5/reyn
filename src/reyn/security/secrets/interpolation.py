"""Generic ${VAR} / $$ environment variable interpolation (ADR-0030).

This is the shared implementation that replaces the MCP-only
``expand_env()`` in ``mcp_client.py``. That module now delegates here
so the resolver logic lives in exactly one place.

Resolution rules
----------------
* ``${VAR}``   → ``os.environ.get("VAR", "")``.  If the variable is not
  set a ``logger.warning`` is emitted (#6145 — promoted from a
  silent-by-default ``warnings.warn``) and the token expands to ``""``.
* ``$$``       → literal ``"$"`` (escape sequence for configs that need a
  literal dollar sign in values without triggering expansion).
* Any other ``$...`` is passed through unchanged.
* The resolver recurses into ``dict`` values and ``list`` items so the
  caller can hand in an entire parsed-YAML config tree.
"""
from __future__ import annotations

import logging
import os
import re
from typing import Any

_log = logging.getLogger(__name__)

# Matches ${VAR_NAME} — word chars only (letters, digits, _).
_ENV_VAR_RE = re.compile(r"\$\{(\w+)\}")


def _expand_str(value: str) -> str:
    """Expand ${VAR} and $$ in a single string value."""
    # Handle $$ → $ first so it is invisible to the VAR regex.
    value = value.replace("$$", "\x00")  # sentinel

    def _replace(m: re.Match) -> str:
        name = m.group(1)
        result = os.environ.get(name)
        if result is None:
            # #6145 A: was `warnings.warn(..., UserWarning)` — silent
            # outside `__main__` under Python's own default filter, and
            # never reached the operator's screen even when visible
            # (`stderr: False / reyn.log: True`, architect's
            # measurement). An operator whose config silently expands to
            # `""` needs this in the log to find the typo.
            _log.warning(
                "Config references undefined environment variable: ${%s}",
                name,
            )
            return ""
        return result

    expanded = _ENV_VAR_RE.sub(_replace, value)
    return expanded.replace("\x00", "$")  # restore literal $


def expand_env(obj: Any) -> Any:
    """Recursively expand ``${VAR}`` in all string values of a dict / list / str.

    Non-string scalars (int, bool, None, …) are returned unchanged.
    """
    if isinstance(obj, str):
        return _expand_str(obj)
    if isinstance(obj, dict):
        return {k: expand_env(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [expand_env(item) for item in obj]
    return obj
