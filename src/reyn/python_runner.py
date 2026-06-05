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

import asyncio
import json
import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from reyn.sandbox.backend import SandboxBackend


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

    async def run(
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
        http_hosts: list[str] | None = None,
        sandbox_write_paths: list[str] | None = None,
        sandbox_backend: "SandboxBackend | None" = None,
        sandbox_policy: dict | None = None,
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

        #571 collapse arc Phase 3: ``http_hosts`` mirrors the same wiring
        for ``reyn.safe.http.*`` per-host gating. Empty = no HTTP via
        safe.http.

        #1352-B: when ``sandbox_backend`` is a real backend (available, not
        noop), the harness subprocess is routed through ``backend.run`` so it
        executes under an OS sandbox (Seatbelt / Landlock / container) — the
        same model as ``sandboxed_exec``. The harness's Python-level
        restricted-builtins stay (defense-in-depth, both layers); any subprocess
        the step spawns (incl. ``reyn.api.unsafe.shell``) is transitively
        contained. ``noop`` / ``None`` → today's direct (unsandboxed) subprocess.
        ``sandbox_policy`` (the agent/operator policy dict) supplies the OS caps;
        its ``timeout_seconds`` is overridden by this step's ``timeout``.
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
            # #571 Phase 3: host allowlist for reyn.safe.http gating.
            "http_hosts": list(http_hosts or []),
        }
        # #1199 S3.4 Part1: forward the phase sandbox write_paths cap (only when
        # set) so reyn.safe.embed_index's host-direct index write self-gates. None
        # (no phase sandbox policy) → key omitted → harness leaves the cap unset.
        if sandbox_write_paths is not None:
            request["sandbox_write_paths"] = list(sandbox_write_paths)

        argv = [self.python_executable, "-m", "reyn.kernel._python_harness"]
        stdin_text = json.dumps(request, ensure_ascii=False)

        # #1352-B: route through the OS sandbox backend when one is configured
        # (real + available + not noop). Falls back to a direct subprocess for
        # noop / None (unchanged behavior — standard chat runs python unsandboxed).
        use_backend = (
            sandbox_backend is not None
            and getattr(sandbox_backend, "name", None) not in (None, "noop")
            and sandbox_backend.available()
        )
        if use_backend:
            stdout_text, returncode, stderr_text, timed_out = await self._run_via_backend(
                sandbox_backend, argv, stdin_text, sandbox_policy, timeout, str(skill_dir),
            )
        else:
            stdout_text, returncode, stderr_text, timed_out = await asyncio.to_thread(
                self._run_direct, argv, stdin_text, timeout,
            )

        if timed_out:
            raise PythonStepError(
                f"python step {module}:{function} timed out after {timeout}s",
                kind="Timeout",
            )

        if not stdout_text:
            # Harness printed nothing (likely crashed before its handler).
            raise PythonStepError(
                f"python step {module}:{function} crashed: "
                f"exit={returncode}, stderr={stderr_text.strip()[:200]}",
                kind="Crash",
            )

        try:
            payload = json.loads(stdout_text)
        except json.JSONDecodeError as exc:
            raise PythonStepError(
                f"python step {module}:{function} returned malformed JSON: "
                f"{stdout_text[:200]}",
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

    def _run_direct(
        self, argv: list[str], stdin_text: str, timeout: int,
    ) -> tuple[str, int, str, bool]:
        """Direct (unsandboxed) harness subprocess — the noop / None path.

        Returns ``(stdout, returncode, stderr, timed_out)``. Synchronous; the
        caller runs it via ``asyncio.to_thread``. Unchanged behavior from before
        #1352-B (standard chat runs python steps unsandboxed)."""
        try:
            proc = subprocess.run(
                argv,
                input=stdin_text,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired:
            return "", -1, "", True
        return proc.stdout, proc.returncode, proc.stderr, False

    async def _run_via_backend(
        self,
        backend: "SandboxBackend",
        argv: list[str],
        stdin_text: str,
        sandbox_policy: dict | None,
        timeout: int,
        cwd: str,
    ) -> tuple[str, int, str, bool]:
        """OS-sandboxed harness subprocess — the real-backend path (#1352-B).

        Builds a SandboxPolicy from the agent/operator policy dict (overriding
        ``timeout_seconds`` with this step's ``timeout``) and routes the harness
        argv through ``backend.run`` (Seatbelt / Landlock / container — uniform
        interface). Returns ``(stdout, returncode, stderr, timed_out)``; the
        backend signals timeout/kill via ``returncode == -1``."""
        from reyn.sandbox import SandboxPolicy

        policy_dict = dict(sandbox_policy or {})
        policy_dict["timeout_seconds"] = timeout
        policy = SandboxPolicy(**policy_dict)

        result = await backend.run(
            argv, policy, stdin=stdin_text.encode("utf-8"), cwd=cwd,
        )
        stdout_text = result.stdout.decode("utf-8", errors="replace")
        stderr_text = result.stderr.decode("utf-8", errors="replace")
        return stdout_text, result.returncode, stderr_text, result.returncode == -1
