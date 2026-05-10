"""Subprocess helpers for ``unsafe``-mode python steps.

Scope A (= this FP): runs inside the python step's subprocess and
calls ``subprocess.run`` directly. Permission for process spawn
was granted at parent level when the step's ``mode: unsafe`` was
approved at startup; individual calls are NOT audited
per-invocation (= step-level audit only). For finer audit see
FP-0015 (deferred).
"""

from __future__ import annotations

import subprocess as _subprocess


def run(
    argv: list[str],
    *,
    cwd: str | None = None,
    env: dict[str, str] | None = None,
    timeout: float = 60,
) -> dict:
    """Run ``argv`` as a subprocess. Returns ``{returncode, stdout, stderr}``.

    ``argv`` is a list (= no shell interpretation). ``stdout`` and
    ``stderr`` are decoded as UTF-8 with ``errors="replace"``.
    Step-level audit (Scope A) — see module docstring.
    """
    completed = _subprocess.run(  # noqa: S603 - intentional, unsafe mode
        argv,
        cwd=cwd,
        env=env,
        timeout=timeout,
        capture_output=True,
        check=False,
    )
    return {
        "returncode": completed.returncode,
        "stdout": completed.stdout.decode("utf-8", errors="replace"),
        "stderr": completed.stderr.decode("utf-8", errors="replace"),
    }
