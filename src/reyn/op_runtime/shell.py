"""shell kind handler — execute a shell command.

Deprecated by FP-0017. Will be removed in 1.0 release. Use sandboxed_exec
instead. The `shell` op invokes `asyncio.create_subprocess_shell` with no
isolation; `sandboxed_exec` routes through a SandboxBackend that enforces
the declared SandboxPolicy.

A `DeprecationWarning` is issued on first invocation per skill (= keyed by
`ctx.skill_name`) to give callers a migration signal without flooding logs.
Stdlib usage of `shell` is zero (verified via grep); custom skills should
migrate to `sandboxed_exec`.
"""
from __future__ import annotations

import asyncio
import warnings
from typing import Literal

from reyn.schemas.models import ShellIROp

from . import register
from .context import OpContext
from .result import OpSkipped

# Tracks which skill_name values have already received the deprecation warning
# in this process. Reset via `_reset_deprecation_for_tests()` if a test needs
# to re-trigger the warning.
_DEPRECATION_WARNED_SKILLS: set[str] = set()


def _reset_deprecation_for_tests() -> None:
    """Test hook: clear the per-skill deprecation latch."""
    _DEPRECATION_WARNED_SKILLS.clear()


def _maybe_warn_deprecated(skill_name: str) -> None:
    """Emit a DeprecationWarning the first time `shell` is used per skill."""
    key = skill_name or "<unknown>"
    if key in _DEPRECATION_WARNED_SKILLS:
        return
    _DEPRECATION_WARNED_SKILLS.add(key)
    warnings.warn(
        "exec op is deprecated; use sandboxed_exec (FP-0017) instead. "
        "Stdlib usage of exec is zero (verified via grep).",
        DeprecationWarning,
        stacklevel=2,
    )


async def handle(op: ShellIROp, ctx: OpContext, caller: Literal["preprocessor", "control_ir"]) -> dict:
    _maybe_warn_deprecated(ctx.skill_name)
    if ctx.permission_resolver is not None:
        if ctx.intervention_bus is None:
            raise RuntimeError("shell op requires intervention_bus on OpContext")
        await ctx.permission_resolver.require_shell(
            ctx.permission_decl, op.cmd, ctx.intervention_bus,
        )
    elif not ctx.shell_allowed:
        raise OpSkipped("shell_not_allowed")

    ctx.events.emit("shell_started", cmd=op.cmd, timeout=op.timeout)
    try:
        proc = await asyncio.create_subprocess_shell(
            op.cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout_b, stderr_b = await asyncio.wait_for(
                proc.communicate(), timeout=op.timeout,
            )
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            ctx.events.emit("shell_timeout", cmd=op.cmd, timeout=op.timeout)
            return {
                "kind": "shell",
                "status": "timeout",
                "returncode": -1,
                "stdout": "",
                "stderr": f"Command timed out after {op.timeout}s",
            }
        stdout = stdout_b.decode("utf-8", errors="replace")
        stderr = stderr_b.decode("utf-8", errors="replace")
        returncode = proc.returncode if proc.returncode is not None else -1
        ctx.events.emit(
            "shell_completed",
            cmd=op.cmd,
            returncode=returncode,
            stdout_len=len(stdout),
            stderr_len=len(stderr),
        )
        return {
            "kind": "shell",
            "status": "ok" if returncode == 0 else "error",
            "returncode": returncode,
            "stdout": stdout,
            "stderr": stderr,
        }
    except OSError as exc:
        return {
            "kind": "shell",
            "status": "error",
            "returncode": -1,
            "stdout": "",
            "stderr": str(exc),
        }


register("shell", handle)
