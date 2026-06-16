"""TUI user preferences persisted to ``<project_root>/.reyn/tui_prefs.json``.

Why a dedicated module:

  - The TUI has a small number of opt-in flags (``/cost-inline``, future
    candidates: default panel tab, banner on/off, etc.) that should
    survive a ``reyn chat`` restart. A flat JSON file under
    ``.reyn/`` is the lowest-friction store (= human-readable, single
    file per project, no schema migrations needed for additive fields).
  - Centralising the load/save here keeps the toggle handlers in
    ``app_outbox.py`` lean: one ``save_tui_prefs(project_root,
    prefs_dict)`` call after mutating, instead of inlining open/json
    boilerplate in every handler.

The JSON shape is intentionally a flat dict — additive keys are
forward-compatible (= an older reyn build reads + writes back
unknown keys unchanged), no migration ceremony required.
"""
from __future__ import annotations

import json
import warnings
from pathlib import Path
from typing import Any


def _prefs_path(project_root: Path) -> Path:
    return project_root / ".reyn" / "tui_prefs.json"


def load_tui_prefs(project_root: Path | None) -> dict[str, Any]:
    """Read ``tui_prefs.json``; return ``{}`` when missing / malformed.

    Defensive: a corrupt file (= JSON parse error, non-object root)
    degrades to an empty prefs dict with a warning. Toggle state
    falls back to the in-code default, no crash on first launch or
    if the file is hand-edited badly.
    """
    if project_root is None:
        return {}
    path = _prefs_path(project_root)
    if not path.exists():
        return {}
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        warnings.warn(
            f"Could not read TUI prefs at {path}: {exc}",
            UserWarning,
            stacklevel=2,
        )
        return {}
    try:
        data = json.loads(raw) if raw.strip() else {}
    except json.JSONDecodeError as exc:
        warnings.warn(
            f"TUI prefs at {path} is not valid JSON ({exc}); ignoring.",
            UserWarning,
            stacklevel=2,
        )
        return {}
    if not isinstance(data, dict):
        warnings.warn(
            f"TUI prefs at {path} must be a JSON object, got "
            f"{type(data).__name__}; ignoring.",
            UserWarning,
            stacklevel=2,
        )
        return {}
    return data


def save_tui_prefs(project_root: Path | None, prefs: dict[str, Any]) -> None:
    """Write ``prefs`` to ``tui_prefs.json``. Silent on best-effort failure.

    A failed write (= read-only FS, permissions) doesn't break the TUI
    — the toggle still applies in-memory; only the persistence fails.
    """
    if project_root is None:
        return
    path = _prefs_path(project_root)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(prefs, indent=2, sort_keys=True),
            encoding="utf-8",
        )
    except OSError as exc:
        warnings.warn(
            f"Could not write TUI prefs to {path}: {exc}",
            UserWarning,
            stacklevel=2,
        )
