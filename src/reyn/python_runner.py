"""Run user-supplied Python preprocessor functions in a subprocess.

The actual function execution lives in `reyn.kernel._python_harness` (child
process). This module is the parent-side wrapper that:

  - resolves the module path relative to a skill's directory and refuses
    paths that escape it
  - launches `python -m reyn.kernel._python_harness` with a JSON request on stdin
  - applies a wall-clock timeout via subprocess.run
  - converts the harness's JSON response (success / failure) into either
    a Python value or a raised PythonStepError

Crash isolation comes "for free" from subprocess: a SIGSEGV / OOM /
SystemExit in the child terminates the child, not reyn itself.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any


class PythonStepError(RuntimeError):
    """Raised when a Python preprocessor step fails for any reason."""

    def __init__(self, message: str, *, kind: str = "Error", traceback: str = "") -> None:
        super().__init__(message)
        self.kind = kind
        self.traceback = traceback


def _resolve_module_path(skill_dir: Path, relative: str) -> Path:
    """Resolve `relative` (e.g. './preprocessing.py') against skill_dir.

    Refuses absolute paths and any path that escapes skill_dir. The
    returned path is absolute and verified to exist.
    """
    if not relative:
        raise PythonStepError("module path is empty")
    rel_path = Path(relative)
    if rel_path.is_absolute():
        raise PythonStepError(
            f"module path must be relative to the skill directory, "
            f"got absolute: {relative!r}"
        )
    candidate = (skill_dir / rel_path).resolve()
    skill_resolved = skill_dir.resolve()
    try:
        candidate.relative_to(skill_resolved)
    except ValueError:
        raise PythonStepError(
            f"module path {relative!r} escapes the skill directory "
            f"{skill_resolved!s} — only paths inside the skill are allowed"
        )
    if not candidate.exists():
        raise PythonStepError(f"module file not found: {candidate}")
    if not candidate.is_file():
        raise PythonStepError(f"module path is not a file: {candidate}")
    return candidate


class PythonRunner:
    """Run user-supplied Python via the harness subprocess.

    Stateless aside from `python_executable`; one runner can serve many
    steps across many phases.
    """

    def __init__(self, python_executable: str | None = None) -> None:
        # Default to the same interpreter reyn itself is running under;
        # this means whatever venv the user has activated for reyn is also
        # what the user code runs against (consistent imports / 3rd-party).
        self.python_executable = python_executable or sys.executable

    def run(
        self,
        *,
        skill_dir: Path,
        module: str,
        function: str,
        mode: str,
        artifact: dict,
        timeout: int,
        allowed_modules: list[str] | None = None,
        file_read_paths: list[str] | None = None,
        file_write_paths: list[str] | None = None,
    ) -> Any:
        """Execute `function` in `module` against `artifact`.

        Returns the function's JSON-roundtripped return value. Raises
        PythonStepError if anything goes wrong: module-not-found, sandbox
        violation, the function raising, JSON serialization failure,
        timeout, or child crash.

        FP-0042: ``file_read_paths`` / ``file_write_paths`` forward the
        skill-declared file permission paths into the subprocess so
        ``reyn.safe.file.*`` calls inside the step can gate against
        them. Both default to empty (= no file access granted) — pass
        the absolute paths the parent has approved for this step.
        """
        module_abs = _resolve_module_path(skill_dir, module)

        request = {
            "module_path": str(module_abs),
            "function": function,
            "mode": mode,
            "artifact": artifact,
            "allowed_modules": list(allowed_modules or []),
            # FP-0042: file-permission paths for reyn.safe.file gating.
            "file_read_paths": list(file_read_paths or []),
            "file_write_paths": list(file_write_paths or []),
        }

        try:
            proc = subprocess.run(
                [self.python_executable, "-m", "reyn.kernel._python_harness"],
                input=json.dumps(request, ensure_ascii=False),
                capture_output=True,
                text=True,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired as exc:
            raise PythonStepError(
                f"python step {module}:{function} timed out after {timeout}s",
                kind="Timeout",
            ) from exc

        if not proc.stdout:
            # Harness printed nothing (likely crashed before its handler).
            raise PythonStepError(
                f"python step {module}:{function} crashed: "
                f"exit={proc.returncode}, stderr={proc.stderr.strip()[:200]}",
                kind="Crash",
            )

        try:
            payload = json.loads(proc.stdout)
        except json.JSONDecodeError as exc:
            raise PythonStepError(
                f"python step {module}:{function} returned malformed JSON: "
                f"{proc.stdout[:200]}",
                kind="MalformedResponse",
            ) from exc

        if payload.get("ok"):
            return payload.get("result")

        # Failure — surface the harness's reported kind/message/traceback.
        kind = payload.get("kind", "Error")
        message = payload.get("error", "unknown error")
        traceback = payload.get("traceback", "")
        raise PythonStepError(
            f"python step {module}:{function} failed ({kind}): {message}",
            kind=kind,
            traceback=traceback,
        )
