"""sandboxed_exec kind handler — execute argv under a SandboxPolicy (FP-0017).

Routes through `reyn.security.sandbox.get_default_backend()` so the OS selects the
appropriate enforcement mechanism per platform. `get_default_backend` auto-
selects SeatbeltBackend (macOS) or LandlockBackend (Linux) where available,
falling back to NoopBackend on unsupported platforms.

Emits `sandboxed_exec_started` / `sandboxed_exec_completed` events (P6).
"""
from __future__ import annotations

import os
from typing import Literal

from reyn.schemas.models import SandboxedExecIROp
from reyn.security.sandbox import SandboxPolicy, get_default_backend
from reyn.security.sandbox.denial import classify_denial
from reyn.security.sandbox.resolve import resolve_real_executable

from . import register
from .context import OpContext


async def handle(
    op: SandboxedExecIROp,
    ctx: OpContext,
) -> dict:
    # FP-0050/#1822 S5 (EP4): exec-scope scan of the command (joined argv) BEFORE
    # any exec. A block-severity hit denies via the permission-deny channel
    # (PermissionError → execute_op status="denied", decision-enabling); a warn
    # emits + proceeds. Orthogonal to the sandbox (which confines exec EFFECTS) —
    # both fire (§4 non-duplication). No-op when threat_scan is absent/disabled.
    _ts = getattr(ctx, "threat_scan", None)
    if _ts is not None and getattr(_ts, "enabled", False):
        from reyn.security.content_guard import first_blocking_match, scan_for_threats
        _matches = scan_for_threats(" ".join(op.argv), _ts, scope="exec")
        for _m in _matches:
            ctx.events.emit(
                "exec_threat_match", pattern_id=_m.pattern_id, severity=_m.severity, scope=_m.scope,
            )
        _block = first_blocking_match(_matches, getattr(_ts, "block_severity", "block"))
        if _block is not None:
            ctx.events.emit(
                "exec_threat_blocked", pattern_id=_block.pattern_id, severity=_block.severity,
            )
            raise PermissionError(
                f"command blocked: matched threat pattern '{_block.pattern_id}' "
                f"(exec/{_block.severity}). Revise the command (avoid pipe-to-shell / "
                f"reverse-shell / homograph URL / terminal-escape) and retry."
            )

    # A runtime backend instance injected on the OpContext takes precedence over
    # name-based platform auto-selection (FP-0008 C7 #2). This lets a caller
    # route exec into a stateful backend (e.g. a Docker container) that the
    # name-based factory cannot build, without the handler knowing the caller.
    backend = ctx.sandbox_backend or get_default_backend(ctx.sandbox_config)
    # #1326: the agent-level (operator) sandbox policy (reyn.yaml sandbox.policy,
    # resolved onto the ctx) WINS over the op's own fields — so the policy is
    # deterministic and the LLM cannot override it. Falls back to the op-level
    # fields when no agent policy is set (unchanged behavior).
    if ctx.default_sandbox_policy is not None:
        policy = SandboxPolicy(**ctx.default_sandbox_policy)
    else:
        policy = SandboxPolicy(
            network=op.network,
            read_paths=list(op.read_paths),
            write_paths=list(op.write_paths),
            allow_subprocess=op.allow_subprocess,
            env_passthrough=list(op.env_passthrough),
            timeout_seconds=op.timeout_seconds,
        )

    # Anchor the working directory to the run's workspace base_dir — parity with
    # the legacy `shell` op (FP-0008 PR-I). Without this, repo-relative `git` /
    # `pytest` run in the harness process cwd instead of the repo root, which
    # breaks concurrent benchmark runs. A workspace-coupled backend (e.g. a
    # container backend) may ignore this host path and use its own baked cwd.
    cwd = str(ctx.workspace.base_dir)

    # #2820 part A: resolve argv[0] past any version-manager shim OUTSIDE the
    # sandbox, so the shim's launch-fork runs in the trusted parent instead of
    # dying under (deny process-fork). The workload's own fork is still denied —
    # only the shim indirection is stripped. Resolution runs `<manager> which`
    # with the child's cwd so the manager picks the version it would for that dir.
    # Fail-open: unchanged argv[0] when resolution is unavailable (the denial then
    # stands, now explained by part B's denial_class). `argv0_resolved` records
    # what actually ran — the tell for a launcher-fork denial that survives.
    env_path = os.environ.get("PATH")
    argv0_resolved = (
        resolve_real_executable(op.argv[0], env_path=env_path, cwd=cwd)
        if op.argv
        else None
    )
    effective_argv = [argv0_resolved, *op.argv[1:]] if op.argv else list(op.argv)

    # #1339: emit the ACTUAL enforced policy values (from the resolved policy),
    # not the op's request fields — the operator-or-default policy wins over op
    # fields, so the trace must show what was enforced (a network:true op under
    # a network:false policy ran WITHOUT network, and the event must say so).
    ctx.events.emit(
        "sandboxed_exec_started",
        argv=list(op.argv),
        argv0_resolved=argv0_resolved,
        backend=backend.name,
        timeout_seconds=policy.timeout_seconds,
        network=policy.network,
        allow_subprocess=policy.allow_subprocess,
    )

    result = await backend.run(
        effective_argv, policy, cwd=cwd, cancel_event=ctx.cancel_event, stdin=op.stdin,
    )

    stdout_text = result.stdout.decode("utf-8", errors="replace")
    stderr_text = result.stderr.decode("utf-8", errors="replace")

    if result.cancelled:
        # #1470: emit distinct event on cancel (P6) — not sandboxed_exec_completed.
        ctx.events.emit(
            "sandboxed_exec_cancelled",
            argv=list(op.argv),
            backend=backend.name,
            returncode=result.returncode,
            stdout_len=len(stdout_text),
            stderr_len=len(stderr_text),
        )
        return {
            "kind": "sandboxed_exec",
            "status": "cancelled",
            "backend": backend.name,
            "returncode": result.returncode,
            "stdout": stdout_text,
            "stderr": stderr_text,
            "truncated": False,
        }

    # #2820: classify a launcher-fork denial (pure fn of returncode+stderr). None
    # for any normal (even nonzero) exit — only a genuine sandbox denial is named,
    # so the canonical layer can tell the LLM "environment/config, not tool
    # availability" and the audit trail records the class.
    denial_class = classify_denial(result.returncode, result.stderr)

    ctx.events.emit(
        "sandboxed_exec_completed",
        argv=list(op.argv),
        argv0_resolved=argv0_resolved,
        backend=backend.name,
        returncode=result.returncode,
        stdout_len=len(stdout_text),
        stderr_len=len(stderr_text),
        truncated=result.truncated,
        denial_class=denial_class,
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
        "denial_class": denial_class,
        "argv0_resolved": argv0_resolved,
    }


from reyn.core.offload.canonical import sandboxed_exec_to_canonical  # noqa: E402

register("sandboxed_exec", handle, canonical=sandboxed_exec_to_canonical)
