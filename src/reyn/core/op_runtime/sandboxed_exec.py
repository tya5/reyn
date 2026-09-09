"""sandboxed_exec kind handler — execute argv under a SandboxPolicy (FP-0017).

Backend resolution and the run+classify tail route through
`reyn.security.sandbox.launcher` (#3823 ①) — the shared slice this handler
and the shell-hook runner both did identically before. `resolve_backend`
wraps `get_default_backend()` so the OS still auto-selects SeatbeltBackend
(macOS) or LandlockBackend (Linux) where available, falling back to
NoopBackend on unsupported platforms; argv0-resolution and the pre-exec
threat scan below stay handler-local (not shared — see the launcher
module's own docstring for why).

Emits `sandboxed_exec_started` / `sandboxed_exec_completed` / (on a
Ctrl-C `cancel_event`, #1470) `sandboxed_exec_cancelled` events (P6).

`run_sandboxed_exec` (#4733 §3-a) is the real body, factored out of the
op-dispatch entry point `handle` so `session_api.run_exec_async` (the
background/async `exec` path) can reuse every line of it, differing only
in the optional `sink` tee callback — see `run_sandboxed_exec`'s own
docstring.

#5838 段4: two mutually-exclusive request shapes, both handled here.
`op.argv` (the original, unchanged form) runs argv0 through the existing
version-manager-shim resolution and the WHOLE-op threat scan. `op.cmd`
(new) is a shell command line: parsed via `security.exec_plan.
parse_exec_plan` and checked via `security.exec_plan_policy.
check_exec_plan_policy` (segment tool-axis + PER-SEGMENT threat scan +
redirect file-axis) BEFORE anything runs — but the string actually
executed is `cmd` UNCHANGED, via `["/bin/sh", "-c", cmd]`, never a
reconstruction from the parsed plan (owner ruling (i), `security/
exec_plan.py`'s own module docstring has the full rationale for why
parse-for-policy/execute-original-string, not the other way around).
`cwd`/`env_path` are resolved ONCE per call and reused for BOTH the
policy check and the actual argv0 resolution — #5991 BLOCKING ③'s own
point carried forward into this stage: the binary policy approved and
the binary that runs must come from the identical PATH/cwd.

Not yet wired: `cmd` is not exposed on the LLM `exec` tool schema or the
pipeline `tool:` step (段6, a separate later stage); `sandboxed_exec_
started`/`_completed`'s own `plan` field recording each segment's
resolved argv[0] (段5) does not exist yet either — only `argv0_resolved`
(always `/bin/sh` for a cmd-mode run) is recorded today.
"""
from __future__ import annotations

import logging
import os
from typing import Callable

from reyn.schemas.models import SandboxedExecIROp
from reyn.security.sandbox import SandboxPolicy
from reyn.security.sandbox.launcher import resolve_backend, run_and_classify
from reyn.security.sandbox.policy import (
    deny_narrowed_write_grants,
    unenforced_axes,
    unenforced_axis_reason,
)
from reyn.security.sandbox.resolve import resolve_real_executable

from . import register
from .context import OpContext

_logger = logging.getLogger(__name__)


async def handle(
    op: SandboxedExecIROp,
    ctx: OpContext,
) -> dict:
    """The op-dispatch entry point (fixed ``(op, ctx)`` signature every
    op-kind handler shares — see ``execute_op``). Thin wrapper over
    :func:`run_sandboxed_exec` with ``sink=None`` — byte-identical to
    before #4733."""
    return await run_sandboxed_exec(op, ctx, sink=None)


async def run_sandboxed_exec(
    op: SandboxedExecIROp,
    ctx: OpContext,
    *,
    sink: "Callable[[int, bytes], None] | None" = None,
) -> dict:
    """The real handler body, factored out of :func:`handle` (#4733 §3-a)
    so ``session_api.run_exec_async`` can reuse EVERY line of this
    handler's logic (threat scan, backend resolution, policy/timeout
    resolution, argv0 resolution, the started/completed/cancelled audit-
    events) for the background/async ``exec`` path, differing ONLY in
    ``sink`` — the callback that tees stdout/stderr to the async runner's
    own file handle as bytes arrive (see ``communicate_capped``'s own
    docstring for the full contract). ``handle`` (the op-dispatch entry
    point above) always passes ``sink=None``, so the SYNCHRONOUS ``exec``
    tool call this handler was written for is completely unaffected by
    this refactor — same behaviour, same return shape, sink simply never
    fires for it."""
    # #5838 段4: cmd-mode's own policy (segment tool-axis + PER-SEGMENT
    # threat scan + redirect file-axis, all via `check_exec_plan_policy`)
    # replaces the whole-op threat scan below for THIS op — the segment
    # scan already covers exec-scope threat matching, more precisely (one
    # scan per segment, not one scan over the whole joined string a
    # chained command's second half could otherwise dilute). See this
    # branch's own comment further down for the parse+policy call itself;
    # `cwd`/`env_path` are computed once, here, and reused by BOTH that
    # call and the (cmd-mode or argv-mode) argv0 resolution below — #5991
    # BLOCKING ③'s own point, carried forward: the binary policy approves
    # and the binary that runs must be resolved from the SAME PATH/cwd.
    cwd = str(ctx.workspace.base_dir)
    env_path = os.environ.get("PATH")

    if op.cmd is not None:
        from reyn.security.exec_plan import ExecPlanRejected, parse_exec_plan
        from reyn.security.exec_plan_policy import check_exec_plan_policy

        try:
            _plan = parse_exec_plan(op.cmd)
        except ExecPlanRejected as _exc:
            return {
                "kind": "sandboxed_exec",
                "status": "error",
                "error": f"command could not be parsed for policy: {_exc}",
            }
        # Raises PermissionError on any segment/redirect denial -- caught
        # uniformly by dispatch_tool's own PermissionError handling
        # (execute_op status="denied"), the SAME channel the threat-scan
        # block below already uses for argv-mode.
        await check_exec_plan_policy(_plan, ctx, env_path=env_path, cwd=cwd)
    else:
        # FP-0050/#1822 S5 (EP4): exec-scope scan of the command (joined argv) BEFORE
        # any exec. A block-severity hit denies via the permission-deny channel
        # (PermissionError → execute_op status="denied", decision-enabling); a warn
        # emits + proceeds. Orthogonal to the sandbox (which confines exec EFFECTS) —
        # both fire (§4 non-duplication). No-op when threat_scan is absent/disabled.
        _ts = getattr(ctx, "threat_scan", None)
        if _ts is not None and getattr(_ts, "enabled", True):  # #4523: shadow default matches ThreatScanConfig.enabled's own declared True
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
    # resolved onto the ctx) is the ONLY source of the enforced policy — the
    # LLM cannot set it via the op (#3907 deleted the 5 op-level policy fields
    # this used to fall back to: #3907① measured every context-building path
    # resolves a concrete `ctx.default_sandbox_policy` — never `None` — so the
    # op-fields fallback branch this comment used to describe was dead code no
    # test could witness without bypassing the real op constructor).
    assert ctx.default_sandbox_policy is not None, (
        "sandboxed_exec: ctx.default_sandbox_policy is None — every real "
        "context-building path resolves a concrete policy (#1339/#3907①); "
        "this op no longer carries policy fields to fall back to, so a "
        "None here is a caller bug, not a recoverable state"
    )
    policy = SandboxPolicy(**ctx.default_sandbox_policy)

    # #3903 a-2 ③ / #4193 ①: which timeout pair applies. ``ctx.ephemeral or
    # not ctx.attended`` — architect ruling, #4193, 2026-08-11. See
    # ``OpContext.attended``'s own docstring for the full 3-state table this
    # approximates ("is a human waiting") and why BOTH disjuncts are load-
    # bearing, not redundant: `ephemeral` alone would miss an unattended
    # persistent spawn (#4193's opening gap); `not attended` alone would
    # break an agent-step leaf worker (ephemeral=True, attended=True — a
    # program, not a human, is waiting via `MessageBus.request`), narrowing
    # it from the background pair to the foreground one and failing any
    # agent step that itself runs a long exec.
    import dataclasses
    if ctx.ephemeral or not ctx.attended:
        effective_default = policy.background_timeout_seconds
        effective_max = policy.background_max_timeout_seconds
    else:
        effective_default = policy.timeout_seconds
        effective_max = policy.max_timeout_seconds

    # #3903① (2026-08-11 owner ruling, architect-conditioned): the LLM may
    # extend the wall-clock timeout past the operator's default
    # (``effective_default`` above), up to the OPERATOR's own configured
    # ceiling (``effective_max`` — never a hardcoded value, so the LLM can
    # never widen an operator's own narrower configuration; ``None`` for
    # the background ceiling means no cap, owner ruling #3903 a-2 — the
    # check below is skipped entirely in that case, there is nothing to
    # exceed). A request above the ceiling is REJECTED (typed error naming
    # the actual max), not silently clamped — a silent clamp would
    # recreate #3962's advertised-but-ignored shape in a new form (the LLM
    # would believe it got the duration it asked for). A non-positive
    # request is also rejected — "wait 0 seconds" is not a meaningful
    # override, and negative durations invert the meaning of the field.
    if op.timeout_seconds is not None:
        if op.timeout_seconds <= 0:
            return {
                "kind": "sandboxed_exec",
                "status": "error",
                "error": (
                    f"timeout_seconds must be positive, got {op.timeout_seconds}"
                ),
            }
        # architect co-vet (#4179): SandboxPolicy.timeout_seconds is int —
        # int(op.timeout_seconds) on a fractional request silently changes
        # the value (0.5 -> 0, an IMMEDIATE timeout; 1.9 -> 1), the exact
        # "the LLM believes it got what it asked for but didn't" shape this
        # whole feature exists to reject, not recreate. Reject a
        # non-integer request outright — same reject-not-silently-change
        # posture already applied above/below, not a special case.
        if op.timeout_seconds != int(op.timeout_seconds):
            return {
                "kind": "sandboxed_exec",
                "status": "error",
                "error": (
                    f"timeout_seconds must be a whole number of seconds, "
                    f"got {op.timeout_seconds}"
                ),
            }
        if effective_max is not None and op.timeout_seconds > effective_max:
            return {
                "kind": "sandboxed_exec",
                "status": "error",
                "error": (
                    f"timeout_seconds ({op.timeout_seconds}) exceeds this "
                    f"deployment's configured maximum of "
                    f"{effective_max} seconds. For a longer-"
                    f"running command, run it in the background instead "
                    f"(spawn an ephemeral session, or run_pipeline with "
                    f"collect=\"async\") — background work runs on a "
                    f"separate budget from this foreground wall-clock cap."
                ),
            }
        policy = dataclasses.replace(policy, timeout_seconds=int(op.timeout_seconds))
    else:
        # The LLM omitted timeout_seconds — apply the resolved DEFAULT for
        # this exec's own pair (ephemeral -> background, else foreground).
        # ``policy.timeout_seconds`` is the ONE field the backend actually
        # reads (below) — this resolves WHICH value flows into it, the
        # backend stays unaware of the fg/bg distinction entirely.
        policy = dataclasses.replace(policy, timeout_seconds=effective_default)

    # `cwd`/`env_path` (the working directory the sandboxed child inherits —
    # parity with the legacy `shell` op, FP-0008 PR-I, so repo-relative
    # `git`/`pytest` resolve against the repo root, not the harness process
    # cwd — and the PATH used to resolve a version-manager shim) were
    # computed ONCE, above, before the threat-scan/policy branch — reused
    # here unchanged for argv0 resolution.

    # #2820 part A: resolve argv[0] past any version-manager shim OUTSIDE the
    # sandbox, so the shim's launch-fork runs in the trusted parent instead of
    # dying under (deny process-fork). The workload's own fork is still denied —
    # only the shim indirection is stripped. Resolution runs `<manager> which`
    # with the child's cwd so the manager picks the version it would for that dir.
    # Fail-open: unchanged argv[0] when resolution is unavailable (the denial then
    # stands, now explained by part B's denial_class). `argv0_resolved` records
    # what actually ran — the tell for a launcher-fork denial that survives.
    #
    # #5838 段4: cmd-mode's argv0 is always `/bin/sh` — the shell that will
    # run *op.cmd* UNCHANGED (owner ruling (i): execute the ORIGINAL string,
    # never a reconstruction from the parsed plan; `security/exec_plan.py`'s
    # own module docstring has the full rationale). `_shell_argv` is what
    # actually reaches the backend AND what the started/completed events
    # below report as `argv` for this run — the parsed plan (already used,
    # above, for policy) is not re-derived into an argv here.
    argv0_resolved: "str | None"
    if op.cmd is not None:
        _shell_argv = ["/bin/sh", "-c", op.cmd]
        argv0_resolved = resolve_real_executable(_shell_argv[0], env_path=env_path, cwd=cwd)
        effective_argv = [argv0_resolved, *_shell_argv[1:]]
        reported_argv = _shell_argv
    elif op.argv:
        argv0_resolved = resolve_real_executable(op.argv[0], env_path=env_path, cwd=cwd)
        effective_argv = [argv0_resolved, *op.argv[1:]]
        reported_argv = list(op.argv)
    else:
        argv0_resolved = None
        effective_argv = list(op.argv)
        reported_argv = list(op.argv)

    # #5825 ①: the op's own REQUEST to run with network enabled (architect
    # ruling, issue #5825, 2026-09-06 — "ask is where a *request* meets a
    # *closed* boundary"). Called ONLY when BOTH are true: the op explicitly
    # asks (op.network is True) AND the resolved policy has it OFF
    # (policy.network is False). A policy that already has network on
    # (compat / unbounded) is untouched — require_network is not called at
    # all, byte-identical to before this field existed (see
    # SandboxedExecIROp.network's own docstring: "the op can REQUEST
    # network, never FORCE it past a narrower operator policy"). On grant,
    # replace THIS call's policy so every downstream read (backend.run, the
    # started/completed events below) sees what was actually enforced —
    # never the op's own unvalidated request field (#1339's own rule,
    # applied to this new axis the same way it already applies to every
    # other one).
    if op.network and not policy.network:
        if ctx.permission_resolver is None:
            raise PermissionError(
                "network access for this exec was requested (network: "
                "true) but no permission resolver is available to gate it."
            )
        await ctx.permission_resolver.require_network(
            ctx.permission_decl, ctx.intervention_bus, ctx.actor,
            argv=reported_argv, agent_name=ctx.agent_name or ctx.actor,
        )
        policy = dataclasses.replace(policy, network=True)

    # #1339: emit the ACTUAL enforced policy values (from the resolved policy),
    # not the op's request fields — the operator-or-default policy wins over op
    # fields, so the trace must show what was enforced (a network:true op under
    # a network:false policy ran WITHOUT network, and the event must say so).
    # #5838 段4: `reported_argv` is `["/bin/sh", "-c", op.cmd]` in cmd-mode
    # (what actually runs), `list(op.argv)` otherwise — never `op.argv`
    # unconditionally, which would show an empty list for every cmd-mode
    # run.
    ctx.events.emit(
        "sandboxed_exec_started",
        argv=reported_argv,
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

    # #3901 §4③ / #3823: the selected backend may not be able to express an
    # axis the policy configured (Landlock cannot carve a read/write deny-list
    # out of an allowed parent — a structural LSM constraint, not a bug).
    # Doc-only visibility reads as "written but nobody checks it" — this
    # makes the gap an audit-event (for later reconstruction) AND a WARN log
    # line (visible at the moment it happens, the same channel
    # sandbox.on_unsupported's own WARN already uses for a backend that is
    # entirely absent — see security/sandbox/__init__.py's _noop_with_policy;
    # that is a DIFFERENT call site, since it fires at backend SELECTION
    # time, before a policy's individual axes are even known, whereas this
    # fires at op DISPATCH time once both the backend and the policy are
    # resolved — #3823's own "same site?" question, answered: no). Not
    # `error` — reyn cannot promise a policy is enforced, only report what a
    # backend actually did with it (owner: "sandbox 抽象は ポリシを 保証できない.
    # backend が できる 範囲で 保証するしか ない"). Deliberately NOT wired into
    # enforcement_self_test (CLAUDE.md hard rule).
    unenforced = unenforced_axes(backend, policy)
    if unenforced:
        reason = unenforced_axis_reason(backend.name)
        ctx.events.emit(
            "sandbox_axis_unenforced",
            backend=backend.name,
            axes=unenforced,
            reason=reason,
        )
        _logger.warning(
            "Sandbox: policy axis(es) %s were configured but the %s backend "
            "cannot enforce them — %s. The policy was written but not "
            "applied for these axes.",
            unenforced,
            backend.name,
            reason,
        )

    launched = await run_and_classify(
        backend, effective_argv, policy, cwd=cwd, cancel_event=ctx.cancel_event, stdin=op.stdin,
        sink=sink,
    )
    result = launched.result

    stdout_text = result.stdout.decode("utf-8", errors="replace")
    stderr_text = result.stderr.decode("utf-8", errors="replace")

    if result.cancelled:
        # #1470: emit distinct event on cancel (P6) — not sandboxed_exec_completed.
        ctx.events.emit(
            "sandboxed_exec_cancelled",
            argv=reported_argv,
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
        argv=reported_argv,
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
