"""sandboxed_exec kind handler — execute argv under a SandboxPolicy (FP-0017).

Routes through `reyn.sandbox.get_default_backend()` so the OS selects the
appropriate enforcement mechanism per platform. Today the default is
NoopBackend (no isolation enforced); future waves add SeatbeltBackend
(macOS) and LandlockBackend (Linux) without touching this handler.

Emits `sandboxed_exec_started` / `sandboxed_exec_completed` events (P6).
"""
from __future__ import annotations

from typing import Literal

from reyn.sandbox import SandboxPolicy, get_default_backend
from reyn.schemas.models import SandboxedExecIROp

from . import register
from .context import OpContext


async def handle(
    op: SandboxedExecIROp,
    ctx: OpContext,
    caller: Literal["preprocessor", "control_ir"],
) -> dict:
    # A runtime backend instance injected on the OpContext takes precedence over
    # name-based platform auto-selection (FP-0008 C7 #2). This lets a caller
    # route exec into a stateful backend (e.g. a Docker container) that the
    # name-based factory cannot build, without the handler knowing the caller.
    backend = ctx.sandbox_backend or get_default_backend(ctx.sandbox_config)
    policy = SandboxPolicy(
        network=op.network,
        read_paths=list(op.read_paths),
        write_paths=list(op.write_paths),
        allow_subprocess=op.allow_subprocess,
        env_passthrough=list(op.env_passthrough),
        timeout_seconds=op.timeout_seconds,
    )

    ctx.events.emit(
        "sandboxed_exec_started",
        argv=list(op.argv),
        backend=backend.name,
        timeout_seconds=op.timeout_seconds,
        network=op.network,
        allow_subprocess=op.allow_subprocess,
    )

    result = await backend.run(list(op.argv), policy)

    stdout_text = result.stdout.decode("utf-8", errors="replace")
    stderr_text = result.stderr.decode("utf-8", errors="replace")

    ctx.events.emit(
        "sandboxed_exec_completed",
        argv=list(op.argv),
        backend=backend.name,
        returncode=result.returncode,
        stdout_len=len(stdout_text),
        stderr_len=len(stderr_text),
        truncated=result.truncated,
    )

    status = "ok" if result.returncode == 0 else ("timeout" if result.returncode == -1 else "error")
    return {
        "kind": "sandboxed_exec",
        "status": status,
        "backend": backend.name,
        "returncode": result.returncode,
        "stdout": stdout_text,
        "stderr": stderr_text,
        "truncated": result.truncated,
    }


register("sandboxed_exec", handle)
