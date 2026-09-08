"""``check_exec_plan_policy`` — #5838 段3: apply policy to an already-parsed
:data:`~reyn.security.exec_plan.ExecPlan`.

段2 (``exec_plan.py``) turns a shell-looking string into a policy-checkable
plan and does NOT check policy. 段4 (a later, separate stage) runs the
ORIGINAL string through ``sh -c`` and does NOT re-derive policy from this
plan. This module is the piece in between: given a plan already produced by
:func:`~reyn.security.exec_plan.parse_exec_plan`, decide whether it is
ALLOWED — raising :class:`PermissionError` when it is not — and does
nothing else. It never executes anything, never re-parses, never mutates
the plan.

## What gets checked, and against which existing primitive (architect's
own plan, #5838 issuecomment-5557210786, point 3 — this module adds no new
primitive, it applies two that already exist elsewhere in production)

- **Every** :class:`~reyn.security.exec_plan.ExecSegment` — its ``argv[0]``,
  resolved past a version-manager shim the SAME WAY
  ``sandboxed_exec.py``'s own ``argv0_resolved`` already is
  (:func:`~reyn.security.sandbox.resolve.resolve_real_executable` — #2820
  part A), then reduced to a basename:
  1. **Tool axis** — the resolved basename is checked against the SAME
     per-session contextual TOOL-axis narrowing ``core.dispatch.dispatcher.
     dispatch_tool`` already applies to a whole tool call (#5841 —
     :func:`~reyn.security.permissions.effective.tool_contextually_denied`
     against ``ctx.contextual_permission``). This is deliberately the exact
     predicate #5841 wired in, not a new one: whatever narrowing already
     excludes a *tool name* for this session (a Profile/topology/
     ``/visibility`` narrowing) excludes the SAME name when it appears as a
     shell segment's resolved binary — closing the basename-detour class
     Codex #28732 named (``./sed`` bypassing a ``sed`` exclusion) at the
     ONE place a bare-argv exclusion already lands.
  2. **Threat scan** — ``content_guard.scan_for_threats`` over the
     segment's own joined argv (``scope="exec"``), the SAME scan
     ``sandboxed_exec.py`` already runs over a whole op's argv (FP-0050/
     #1822 S5) — run per SEGMENT here (a chained command's second half is
     scanned independently, not hidden inside a joined string the first
     half's benign wording could dilute).
- **Every** :class:`~reyn.security.exec_plan.ExecRedirect` — its ``path``,
  through the EXISTING file-axis primitive production already uses in 8
  other places (``op_runtime/file.py`` and friends — architect's own plan:
  "新設しない"): ``PermissionResolver.require_file_write`` for ``>``/``>>``,
  ``require_file_read`` for ``<``. No new file-permission concept.

## What this module deliberately does NOT do

- **Does not execute anything.** A plan that passes every check here is
  still just a plan — 段4 runs the ORIGINAL string, not this one.
- **Does not decide what happens when a segment is denied.** Every
  competitor #5838's own research surveyed (Claude Code / Codex) escalates
  an unresolvable or denied construct TO THE OPERATOR rather than only
  refusing (architect's own competitive doc already names this: "人に回
  す"). reyn's ``dispatch_tool``/``require_file_*`` gates this module reuses
  raise on denial and stop there — there is no "ask" leg wired for a
  segment-level denial yet. **Deliberately left as an open design
  question for architect to rule on** (lead-coder, PR #5984's own
  TESTS-READ: "「拒否 → operator に尋ねる」の設計は architect に諮る形で
  提案してください") — not decided by this PR. Proposal, for architect's
  ruling: FP-0069's own ``reviewed`` JIT-confirm leg (the SAME mechanism
  ``require_file_write``/``require_file_read`` already use via
  ``ctx.intervention_bus`` — a segment redirect denial can already reach a
  human this way when a bus is wired) is the natural home for the
  TOOL-axis half too, rather than a THIRD ask-mechanism; today's
  ``tool_contextually_denied`` predicate has no bus parameter at all
  (#5841's own docstring: "deliberately does NOT wire ``require_tool``'s
  confirmation half... wiring a confirmation here independently would
  create a second source for that same decision") — extending it is out
  of THIS module's scope and needs its own ruling, not a quiet addition
  here.
- **Does not gate the plan's SHAPE.** Which constructs are even
  representable (a heredoc, `$(...)`, an env-assignment prefix) was
  decided by ``parse_exec_plan`` (段2) already, before this module ever
  sees a plan — nothing here second-guesses that.
"""
from __future__ import annotations

import os
from typing import TYPE_CHECKING

from reyn.security.exec_plan import ExecRedirect, ExecSegment
from reyn.security.permissions.effective import (
    contextual_deny_message,
    gate_effective_tool_name,
    tool_contextually_denied,
)
from reyn.security.sandbox.resolve import resolve_real_executable

if TYPE_CHECKING:
    from reyn.core.op_runtime.context import OpContext
    from reyn.security.exec_plan import ExecPlan


async def check_exec_plan_policy(plan: "ExecPlan", ctx: "OpContext") -> None:
    """Apply 段3 policy to every item of *plan* — see this module's own
    docstring for exactly what each item type is checked against. Raises
    :class:`PermissionError` on the FIRST denial encountered, in plan
    order (left to right, the same order the items appeared in the
    original text) — never collects every violation, matching every other
    ``require_*`` gate in this codebase (``file.py``'s own sequential
    write/read checks, ``sandboxed_exec.py``'s own single threat-scan
    raise). Returns ``None`` when every item passes.

    Never executes anything, never re-parses *plan* — a pure policy
    check over an already-parsed :data:`~reyn.security.exec_plan.
    ExecPlan`."""
    env_path = os.environ.get("PATH")
    cwd = str(ctx.workspace.base_dir) if ctx.workspace is not None else None
    for item in plan:
        if isinstance(item, ExecSegment):
            await _check_segment(item, ctx, env_path=env_path, cwd=cwd)
        elif isinstance(item, ExecRedirect):
            await _check_redirect(item, ctx)
        # ExecChainOp carries no policy-relevant data of its own — the
        # segments either side of it are checked independently.


async def _check_segment(
    segment: "ExecSegment", ctx: "OpContext", *, env_path: "str | None", cwd: "str | None"
) -> None:
    """Tool-axis + threat-scan check for one segment — see this module's
    own docstring, "What gets checked", point 1/2."""
    if not segment.argv:
        # Unreachable via parse_exec_plan (an empty segment is rejected at
        # parse time — exec_plan.py's own _flush_segment), kept as a
        # defensive no-op rather than an IndexError for any other future
        # ExecPlan producer.
        return

    # #2820 part A (the SAME resolution sandboxed_exec.py's own
    # argv0_resolved already performs): strip a version-manager shim
    # indirection, filesystem-only, no subprocess. Reduced to a basename
    # because the tool-axis narrowing this feeds (below) names tools by
    # bare name ("exec", "grep", ...), never an absolute path.
    argv0_resolved = resolve_real_executable(segment.argv[0], env_path=env_path, cwd=cwd)
    resolved_name = os.path.basename(argv0_resolved)

    effective = gate_effective_tool_name(resolved_name, None)
    contextual = getattr(ctx, "contextual_permission", None)
    if effective is not None and tool_contextually_denied(contextual, effective):
        raise PermissionError(
            contextual_deny_message("command", effective, contextual)
        )

    threat_scan = getattr(ctx, "threat_scan", None)
    if threat_scan is not None and getattr(threat_scan, "enabled", True):
        from reyn.security.content_guard import first_blocking_match, scan_for_threats

        matches = scan_for_threats(" ".join(segment.argv), threat_scan, scope="exec")
        for match in matches:
            ctx.events.emit(
                "exec_threat_match",
                pattern_id=match.pattern_id, severity=match.severity, scope=match.scope,
            )
        block = first_blocking_match(matches, getattr(threat_scan, "block_severity", "block"))
        if block is not None:
            ctx.events.emit(
                "exec_threat_blocked", pattern_id=block.pattern_id, severity=block.severity,
            )
            raise PermissionError(
                f"command blocked: matched threat pattern '{block.pattern_id}' "
                f"(exec/{block.severity}). Revise the command (avoid pipe-to-shell / "
                f"reverse-shell / homograph URL / terminal-escape) and retry."
            )


async def _check_redirect(redirect: "ExecRedirect", ctx: "OpContext") -> None:
    """File-axis check for one redirect target — see this module's own
    docstring, "What gets checked", the ``ExecRedirect`` bullet. Mirrors
    ``op_runtime/file.py``'s own ``if ctx.permission_resolver is not
    None:`` guard exactly — no resolver wired means no check, the SAME
    fail-open every other ``require_file_*`` call site in this codebase
    already accepts (a resolver-less ``OpContext`` is a real, supported
    construction — tests and some non-interactive callers)."""
    if ctx.permission_resolver is None:
        return

    from reyn.core.op_runtime.context import resolve_path_for_gate, sandbox_policy_from_ctx

    resolved_path = resolve_path_for_gate(ctx, redirect.path)
    sandbox = sandbox_policy_from_ctx(ctx)
    if redirect.op in (">", ">>"):
        await ctx.permission_resolver.require_file_write(
            ctx.permission_decl, resolved_path, ctx.actor,
            sandbox_policy=sandbox, bus=ctx.intervention_bus,
        )
    else:  # "<"
        await ctx.permission_resolver.require_file_read(
            ctx.permission_decl, resolved_path, ctx.actor,
            sandbox_policy=sandbox, bus=ctx.intervention_bus,
        )
