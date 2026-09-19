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

#6224 revision — the original version returned a bare `bool` that was
`True` the instant the LAUNCHER process itself started, never whether the
opened application actually succeeded. That conflated two different
facts into one bit and, worse, never even checked the launcher's OWN exit
code — `subprocess.Popen(...)` was never waited on, so a launcher that
exited immediately with a real, user-legible error (macOS `open`: no
handler for the extension, file missing, `-a` app missing — all `rc=1`
with a descriptive stderr line, verified on real hardware) was silently
reported as success. `OpenResult` below replaces the bare `bool` with a
typed 3-state result (project convention: typed discriminated union over
a form-sniffed value) that is honest about which of these three distinct
facts is actually known:

  * ``"launch_failed"`` — the launcher itself never started (a missing
    opener binary, a permission error, ...).
  * ``"failed"`` — a DEFINITE, known failure: either a pre-launch check
    caught it (the target does not exist — see below), or, on macOS only,
    the launcher's own exit code was non-zero.
  * ``"accepted"`` — the launcher was invoked and did not report a known
    failure. **This is not "opened".** Whether the OS's own resolved
    handler actually succeeded once it started is a fact this process has
    no way to observe on ANY platform (same residue `copy_to_clipboard`
    carries for its own subprocess boundary) — never render `accepted` as
    a success claim; a silent, non-committal state satisfies that (lead-
    coder ruling, #6224: a status line printed on every ordinary success
    would be a different regression — the Linux user would gain zero new
    information from it, since accepted was already the ambient case).

Pre-launch checks (target existence via `Path.exists()`, opener-binary
presence via `shutil.which()`) run identically on every platform, BEFORE
the launcher is ever invoked, and short-circuit straight to `"failed"`
without touching the launcher at all. This is deliberate, not merely
convenient: `xdg-open`'s own documented exit codes name exactly these two
failure shapes (`2` = file not found, `3` = required tool not found) as
things knowable without waiting for it to return — and Linux MUST NOT
wait for `xdg-open` to return (see below), so this is the only way this
module can ever surface either fact there. Running the SAME two checks on
macOS too — even though `open`'s own exit code could also report them —
keeps one fact behind exactly one message: whatever the pre-check
rejects never reaches the launcher, so the macOS `rc != 0` branch only
ever carries "passed the pre-check, launcher itself still reported
failure" — never the same fact twice under two different wordings.

**Residual, disclosed as a NAME, not "fixed"**: on Linux, this closes 2
of `xdg-open`'s 4 documented failure exit codes (`2` file-not-found, `3`
tool-not-found) via the pre-launch checks above. The remaining 2 (`1`
command-line syntax — not reachable, this module's own argv is fixed
shape; `4` "action failed", i.e. xdg-open found a handler and invoking it
failed) stay genuinely invisible: this module deliberately does NOT
`wait()` on `xdg-open`, because several real implementations (Electron:
https://github.com/electron/electron/pull/10902, sindresorhus/open)
report that `xdg-open` can stay attached to a terminal-based handler and
never return at all — waiting would trade a silent failure for a frozen
TUI, a strictly worse regression. macOS does not carry this residual: real
hardware measurement (lead-coder, #6224) confirms `open` always forks and
returns quickly across all 3 failure shapes tested, so waiting on IT
(never on the application it launches) is safe, and its exit code +
stderr are surfaced via `OpenResult.detail`.
"""
from __future__ import annotations

import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Literal


@dataclass(frozen=True)
class OpenResult:
    """See the module docstring for what each ``state`` does and does not
    claim to know. ``detail`` is a human-legible reason, present only for
    ``"failed"``/``"launch_failed"`` (``None`` for ``"accepted"`` — there is
    nothing to report)."""

    state: Literal["launch_failed", "accepted", "failed"]
    detail: "str | None" = None


def open_with_os_default(path: "str | Path") -> OpenResult:
    """Launch `path` with the OS's own default application for its
    extension. Never raises. See the module docstring for the 3-state
    result this returns and exactly what each state does and does not
    mean."""
    target = str(path)

    # Pre-launch checks — same on every platform, before the launcher is
    # ever invoked (module docstring: one fact, one message).
    if not Path(target).exists():
        return OpenResult("failed", f"{target!r} does not exist")

    try:
        if sys.platform == "darwin":
            opener = "open"
            if shutil.which(opener) is None:
                return OpenResult("failed", f"{opener!r} not found on PATH")
            proc = subprocess.Popen(
                [opener, target], stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE, text=True,
            )
            # `open` itself always forks and returns quickly — real-hardware
            # verified (lead-coder, #6224) across 3 failure shapes. This
            # waits on the LAUNCHER, never on the application it starts.
            _, stderr = proc.communicate()
            if proc.returncode != 0:
                detail = stderr.strip() or f"open exited {proc.returncode}"
                return OpenResult("failed", detail)
            return OpenResult("accepted")
        elif sys.platform == "win32":
            import os
            os.startfile(target)  # type: ignore[attr-defined]
            return OpenResult("accepted")
        else:
            opener = "xdg-open"
            if shutil.which(opener) is None:
                return OpenResult("failed", f"{opener!r} not found on PATH")
            # Deliberately NOT waited — xdg-open may stay attached to a
            # terminal-based handler and block indefinitely (module
            # docstring). Residual: an xdg-open exit-4 ("action failed")
            # stays invisible here.
            subprocess.Popen(
                [opener, target], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            return OpenResult("accepted")
    except Exception as exc:
        return OpenResult("launch_failed", str(exc))


__all__ = ["OpenResult", "open_with_os_default"]
