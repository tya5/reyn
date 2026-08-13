"""Open a local file with the OS's own default application (#4482 PR-3) —
the same affordance nvim's `gx` or a file manager double-click give, never
a reyn-chosen viewer.

Sibling to `_clipboard.py` (same module, same shape): a thin per-platform
dispatch to the OS's own opener (`open` / `xdg-open` / `os.startfile`),
never a reyn-maintained handler table. `open`/`xdg-open`/`start` all pick
the handler FROM THE FILE'S EXTENSION — architect's #4482 review named
this explicitly: "拡張子が権限の実体である" (the extension IS the
permission surface). This module does not — and must not — second-guess
that: it launches the OS's own resolved handler, whatever that is on this
machine, for whatever extension the file actually has. A `.pptx` opening
in an office suite with macro support is that suite's own capability, not
something this function grants or could withhold — nvim's `gx` carries
the identical residue and does not gate on it either.

Returns whether the OS ACCEPTED the request (the process launched) — never
whether the opened application itself succeeded internally, which this
process has no visibility into (same success bar as `copy_to_clipboard`).
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path


def open_with_os_default(path: "str | Path") -> bool:
    """Launch `path` with the OS's own default application for its
    extension. Never raises — a missing opener binary, a nonexistent path,
    or any other launch failure returns `False` rather than propagating,
    matching `copy_to_clipboard`'s own established failure contract (the
    caller reports it, this function just tells whether it happened)."""
    target = str(path)
    try:
        if sys.platform == "darwin":
            subprocess.Popen(
                ["open", target], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
        elif sys.platform == "win32":
            import os
            os.startfile(target)  # type: ignore[attr-defined]
        else:
            subprocess.Popen(
                ["xdg-open", target], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
        return True
    except Exception:
        return False


__all__ = ["open_with_os_default"]
