"""sandboxed_exec kind handler — execute argv under a SandboxPolicy (FP-0017).

Backend resolution and the run+classify tail route through
`reyn.security.sandbox.launcher` (#3823 ①) — the shared slice this handler
and the shell-hook runner both did identically before. `resolve_backend`
wraps `get_default_backend()` so the OS still auto-selects SeatbeltBackend
(macOS) or LandlockBackend (Linux) where available, falling back to
NoopBackend on unsupported platforms; argv0-resolution and the pre-exec
threat scan below stay handler-local (not shared — see the launcher
module's own docstring for why).

Emits `sandboxed_exec_started` / `sandboxed_exec_completed` events (P6).
"""
from __future__ import annotations

import os
from typing import Literal

from reyn.schemas.models import SandboxedExecIROp
from reyn.security.sandbox import SandboxPolicy
from reyn.security.sandbox.launcher import resolve_backend, run_and_classify
from reyn.security.sandbox.policy import deny_narrowed_write_grants, unenforced_axes
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
    backend = resolve_backend(ctx.sandbox_backend, ctx.sandbox_config)
    # #1326: the agent-level (operator) sandbox policy (reyn.yaml sandbox.policy,
    # resolved onto the ctx) WINS over the op's own fields — so the policy is
    # deterministic and the LLM cannot override it. Falls back to the op-level
    # fields when no agent policy is set (unchanged behavior).
    if ctx.default_sandbox_policy is not None:
        policy = SandboxPolicy(**ctx.default_sandbox_policy)
    else:
        # #3901 PR-B: `op` (SandboxedExecIROp) and `SandboxPolicy` are
        # DIFFERENT vocabularies now, not a naming accident — op is what the
        # LLM requests ("let me do X", allow_*), policy is what the
        # operator forbids ("don't let it Y", deny_*). Direct field-by-field
        # translation here (not a shared conversion layer: this is the
        # ONLY production construction site — #3907 tracks these op fields
        # having zero real producers; every setter found was a test
        # constructing the op directly). `op.read_paths` has no policy
        # counterpart (removed #3901 PR-B ④, broad-read realignment made it
        # dead everywhere); `op.env_passthrough` has no direct translation
        # (an allow-list of names vs `env_deny_names`' a deny-list means
        # "block nothing extra" IS its empty default, so an empty
        # `env_passthrough` — the only value #3907 found ever produced —
        # needs no translation at all).
        policy = SandboxPolicy(
            network=op.network,
            write_paths=list(op.write_paths),
            deny_subprocess=not op.allow_subprocess,
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
        # #3901 PR-B ④: field renamed (allow_subprocess -> deny_subprocess,
        # inverted sense) — this event's data key follows the policy's own
        # field name, not a fixed audit-event kind (no AUDIT_EVENT_KINDS
        # three-point-set implication; the kind itself is unchanged).
        deny_subprocess=policy.deny_subprocess,
    )

    # #2978: deny-always-wins — when a write_deny_paths entry overlaps a
    # write_paths grant (field renamed #3901 PR-B ④: this used to check
    # read_deny_paths, an undocumented Seatbelt side-effect closed by giving
    # write its own explicit deny-list), the backend now lets the deny win.
    # Emit an audit-event so the narrowing is observable, not silent. This is
    # the LLM-reachable path (op-authored / operator-resolved write_paths
    # reach here via ctx.default_sandbox_policy); the enforcement itself
    # lives at the Seatbelt emit chokepoint, so every policy-construction
    # path is covered.
    narrowed = deny_narrowed_write_grants(policy)
    if narrowed:
        ctx.events.emit(
            "sandbox_policy_narrowed",
            backend=backend.name,
            narrowed=[
                {"write_path": w, "deny_path": d} for w, d in narrowed
            ],
        )

    # #3901 §4③: the selected backend may not be able to express an axis the
    # policy configured (Landlock cannot carve a read/write deny-list out of
    # an allowed parent — a structural LSM constraint, not a bug). Doc-only
    # visibility reads as "written but nobody checks it" — this makes the gap
    # an audit-event instead, mirroring sandbox_policy_narrowed's precedent.
    # Deliberately NOT wired into enforcement_self_test (CLAUDE.md hard rule).
    unenforced = unenforced_axes(backend.name, policy)
    if unenforced:
        ctx.events.emit(
            "sandbox_axis_unenforced",
            backend=backend.name,
            axes=unenforced,
        )

    launched = await run_and_classify(
        backend, effective_argv, policy, cwd=cwd, cancel_event=ctx.cancel_event, stdin=op.stdin,
    )
    result = launched.result

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

    # #2820: a launcher-fork denial (pure fn of returncode+stderr). None for
    # any normal (even nonzero) exit — only a genuine sandbox denial is
    # named, so the canonical layer can tell the LLM "environment/config,
    # not tool availability" and the audit trail records the class.
    # Classified inside run_and_classify (#3823 ①, the shared backend-resolve
    # + run() + classify_denial slice both sandboxed_exec and the shell-hook
    # runner already did identically) — reused here, not re-derived.
    denial_class = launched.denial_class

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
