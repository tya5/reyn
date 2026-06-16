"""Generic ${VAR} / $$ environment variable interpolation (ADR-0030).

This is the shared implementation that replaces the MCP-only
``expand_env()`` in ``mcp_client.py``. That module now delegates here
so the resolver logic lives in exactly one place.

Resolution rules
----------------
* ``${VAR}``   → ``os.environ.get("VAR", "")``.  If the variable is not
  set a :class:`UserWarning` is emitted and the token expands to ``""``.
* ``$$``       → literal ``"$"`` (escape sequence for configs that need a
  literal dollar sign in values without triggering expansion).
* Any other ``$...`` is passed through unchanged.
* The resolver recurses into ``dict`` values and ``list`` items so the
  caller can hand in an entire parsed-YAML config tree.
"""
from __future__ import annotations

import os
import re
import warnings
from typing import Any

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
            warnings.warn(
                f"Config references undefined environment variable: ${{{name}}}",
                UserWarning,
                stacklevel=4,
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
