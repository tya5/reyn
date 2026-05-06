"""shell kind handler — execute a shell command."""
from __future__ import annotations

import asyncio
from typing import Literal

from reyn.schemas.models import ShellIROp

from . import register
from .context import OpContext
from .result import OpSkipped


async def handle(op: ShellIROp, ctx: OpContext, caller: Literal["preprocessor", "control_ir"]) -> dict:
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
