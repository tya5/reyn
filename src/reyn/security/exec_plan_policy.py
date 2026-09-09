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

## #5991 BLOCKING (architect co-vet, issuecomment-5578916234) — 3 findings,
same shape: the surface this module checked did not match the surface
that will actually matter once 段4 lands.

1. **No-resolver redirect skip was fail-OPEN, not fail-closed.** The
   earlier version mirrored ``file.py``'s own ``if ctx.permission_resolver
   is not None:`` guard as a "parity" argument — but that parity does not
   hold: ``file.py``'s guard has real production callers today (a
   resolver-less ``OpContext`` IS a supported, exercised construction
   there); THIS gate has ZERO callers yet (段4 is unbuilt), so there is no
   existing population this fail-open behaviour protects, and fixing it
   now costs nothing. A missing resolver now RAISES (fail-closed) — see
   :func:`_check_redirect`.
2. **"Zero threat matches" and "the scan never ran" were indistinguishable
   in ``.reyn/events``.** ``ctx.threat_scan`` defaults to ``None``, and the
   only events this module emitted (``exec_threat_match``/
   ``exec_threat_blocked``) fire ONLY on a match — a plan that was never
   scanned at all leaves the SAME empty trace as one that was scanned and
   found clean. This module now emits exactly ONE event per segment
   recording what happened either way (``exec_threat_scanned`` when the
   scan ran, ``exec_threat_scan_skipped`` when it did not) — see
   :func:`_check_segment`.
3. **``argv[0]`` resolution read ambient ``os.environ["PATH"]``, not the
   env a sandboxed run will actually see.** ``sandbox/seatbelt.py:425``
   only falls back to ``os.environ`` when the caller passes no explicit
   PATH — 段4's own sandbox env can differ, and resolving against the
   wrong one reproduces the EXACT class #5984 found 4 instances of, one
   layer down: the binary this module approves and the binary that
   actually runs could be two different files. :func:`check_exec_plan_
   policy` now takes BOTH ``env_path`` AND ``cwd`` (a version-manager's
   per-directory config is read from ``cwd`` too — the same resolution
   input, same risk) as REQUIRED keyword-only arguments (no internal
   fallback to ``os.environ``, no internal derivation from
   ``ctx.workspace``) — every caller, present and future, must decide and
   pass the real values; there is no default to silently get wrong,
   omitting either is a ``TypeError`` at the call site, not a runtime
   surprise later (lead-coder co-vet, issuecomment-5579159401: "cwd も
   同じ").

## #6007 BLOCKING (architect co-vet, issuecomment-5580912677, found while
reviewing 段4) — a redirect's own ``path`` was never threat-scanned.
Argv-mode's whole-op scan (``sandboxed_exec.py``) sees every token
including a redirect target (``" ".join(op.argv)``); this module's own
per-segment scan never touched :attr:`ExecRedirect.path` at all — a
surface UNIQUE to going through this module (cmd-mode) that was left
unscanned only there. :func:`_check_redirect` now runs the SAME scan
(:func:`_run_threat_scan`, factored out of :func:`_check_segment`) over
the redirect's own path, before the resolver-presence check (scanning
needs no resolver).

## #5838 段5 (lead-coder ruling, #5838 issue thread) — audit needs to
name what actually ran. Charter lens 7 (Observability — an audit-event
trace must be sufficient to reconstruct what happened): a cmd-mode run's
``argv0_resolved`` is ALWAYS ``/bin/sh`` (段4's own shape), so nothing in
``.reyn/events`` said which binary a chained/piped command actually
invoked. :func:`check_exec_plan_policy` now RETURNS the resolved
``argv[0]`` for every segment it checks — the value it already computes
internally for the tool-axis check — so ``sandboxed_exec.py`` can build
its own ``sandboxed_exec_started``/``_completed`` ``plan`` field from
THIS list, never by calling :func:`~reyn.security.sandbox.resolve.
resolve_real_executable` a second time (the exact "policy saw one
binary, audit recorded a different one" class #5991 BLOCKING ③ closed
for ``env_path``/``cwd`` — now closed for the resolved name too).
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


async def check_exec_plan_policy(
    plan: "ExecPlan", ctx: "OpContext", *, env_path: "str | None", cwd: "str | None"
) -> "list[dict[str, str]]":
    """Apply 段3 policy to every item of *plan* — see this module's own
    docstring for exactly what each item type is checked against. Raises
    :class:`PermissionError` on the FIRST denial encountered, in plan
    order (left to right, the same order the items appeared in the
    original text) — never collects every violation, matching every other
    ``require_*`` gate in this codebase (``file.py``'s own sequential
    write/read checks, ``sandboxed_exec.py``'s own single threat-scan
    raise).

    Returns ``[{"argv0": <original>, "resolved": <resolved absolute
    path>}, ...]``, one entry per :class:`~reyn.security.exec_plan.
    ExecSegment` in *plan*, in order — #5838 段5 (lead-coder ruling,
    #5838), the pairing added on architect's own PR co-vet suggestion
    (issuecomment-5594370219: recording ONLY the resolved value would
    force a reader of ``sandboxed_exec_started``'s ``plan`` field to
    re-parse ``cmd`` to know what each segment's ORIGINAL ``argv[0]`` was
    — the pair also preserves whether/what a version-manager shim
    resolved to, e.g. ``{"argv0": "python", "resolved": "/opt/.pyenv/
    versions/3.12/bin/python"}``). ``resolved`` is the same absolute path
    :func:`~reyn.security.sandbox.resolve.resolve_real_executable`
    produced, already computed here for the tool-axis check — a caller
    building the audit trace reuses THIS list rather than calling
    ``resolve_real_executable`` a second time, the exact "policy saw one
    binary, the trace recorded a different one" class #5991 BLOCKING ③
    closed for env_path/cwd, now closed for the resolved name itself.
    ``ExecChainOp``/``ExecRedirect`` entries contribute nothing to this
    list (they have no argv[0] of their own).

    *env_path*/*cwd* are the ``PATH``/working-directory the eventual
    sandboxed run will actually see — BOTH REQUIRED, keyword-only, no
    internal fallback (#5991 BLOCKING ③, this module's own docstring):
    every caller must decide the real values (``sandboxed_exec.py``'s own
    ``env_path = os.environ.get("PATH")`` / ``cwd = str(ctx.workspace.
    base_dir)``, once 段4 wires this module in, is the pair to reuse)
    rather than let this function silently resolve against values that
    may not match what actually executes.

    Never executes anything, never re-parses *plan* — a pure policy
    check over an already-parsed :data:`~reyn.security.exec_plan.
    ExecPlan`."""
    resolved_argv0: "list[dict[str, str]]" = []
    for item in plan:
        if isinstance(item, ExecSegment):
            resolved = await _check_segment(item, ctx, env_path=env_path, cwd=cwd)
            resolved_argv0.append({"argv0": item.argv[0] if item.argv else "", "resolved": resolved})
        elif isinstance(item, ExecRedirect):
            await _check_redirect(item, ctx)
        # ExecChainOp carries no policy-relevant data of its own — the
        # segments either side of it are checked independently.
    return resolved_argv0


async def _check_segment(
    segment: "ExecSegment", ctx: "OpContext", *, env_path: "str | None", cwd: "str | None"
) -> str:
    """Tool-axis + threat-scan check for one segment — see this module's
    own docstring, "What gets checked", point 1/2. Returns the RESOLVED
    ``argv[0]`` (#5838 段5 — see :func:`check_exec_plan_policy`'s own
    docstring for why this is returned rather than re-derived by a
    caller)."""
    if not segment.argv:
        # Unreachable via parse_exec_plan (an empty segment is rejected at
        # parse time — exec_plan.py's own _flush_segment), kept as a
        # defensive no-op rather than an IndexError for any other future
        # ExecPlan producer. "" (not None) keeps the return type a plain
        # str, matching every reachable path.
        return ""

    # #2820 part A (the SAME resolution sandboxed_exec.py's own
    # argv0_resolved already performs): strip a version-manager shim
    # indirection, filesystem-only, no subprocess. Reduced to a basename
    # because the tool-axis narrowing this feeds (below) names tools by
    # bare name ("exec", "grep", ...), never an absolute path — the
    # UNREDUCED `argv0_resolved` (this function's own return value) is
    # what a caller building an audit trace wants instead (matches
    # `sandboxed_exec.py`'s own existing `argv0_resolved` field shape).
    argv0_resolved = resolve_real_executable(segment.argv[0], env_path=env_path, cwd=cwd)
    resolved_name = os.path.basename(argv0_resolved)

    effective = gate_effective_tool_name(resolved_name, None)
    contextual = getattr(ctx, "contextual_permission", None)
    if effective is not None and tool_contextually_denied(contextual, effective):
        raise PermissionError(
            contextual_deny_message("command", effective, contextual)
        )

    await _run_threat_scan(ctx, " ".join(segment.argv), subject=list(segment.argv))
    return argv0_resolved


async def _run_threat_scan(ctx: "OpContext", text: str, *, subject: "list[str]") -> None:
    """The threat-scan half of a segment's own check, factored out so
    :func:`_check_redirect` can apply the IDENTICAL scan to a redirect's
    own ``path`` (#6007 BLOCKING, architect co-vet, issuecomment-
    5580912677: argv-mode's whole-op scan sees every token including a
    redirect target — ``" ".join(op.argv)`` in ``sandboxed_exec.py`` —
    but cmd-mode's per-segment scan never touched ``ExecRedirect.path``
    at all, a surface UNIQUE to cmd-mode that was left unscanned only in
    cmd-mode). *subject* is what the emitted event's own ``argv`` field
    names — a segment's own argv, or a redirect's path wrapped in a
    single-element list — never re-derived from *text* (a redirect path
    with an embedded space must not be miscounted as multiple tokens).

    Raises :class:`PermissionError` on a block-severity match; always
    emits exactly ONE of ``exec_threat_scanned``/``exec_threat_scan_
    skipped`` (#5991 BLOCKING ②'s own "two zeros" fix, unchanged here)."""
    threat_scan = getattr(ctx, "threat_scan", None)
    scan_will_run = threat_scan is not None and getattr(threat_scan, "enabled", True)
    if not scan_will_run:
        ctx.events.emit(
            "exec_threat_scan_skipped",
            argv=subject,
            reason="disabled" if threat_scan is not None else "not_configured",
        )
        return

    from reyn.security.content_guard import first_blocking_match, scan_for_threats

    matches = scan_for_threats(text, threat_scan, scope="exec")
    for match in matches:
        ctx.events.emit(
            "exec_threat_match",
            pattern_id=match.pattern_id, severity=match.severity, scope=match.scope,
        )
    block = first_blocking_match(matches, getattr(threat_scan, "block_severity", "block"))
    ctx.events.emit(
        "exec_threat_scanned",
        argv=subject, match_count=len(matches), blocked=block is not None,
    )
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
    docstring, "What gets checked", the ``ExecRedirect`` bullet.

    #5991 BLOCKING ①: an earlier version mirrored ``op_runtime/file.py``'s
    own ``if ctx.permission_resolver is not None:`` fail-OPEN guard as a
    "parity" argument — that parity does not hold here. ``file.py``'s
    fail-open protects a real, exercised population of resolver-less
    callers TODAY; this gate has ZERO callers yet (段4, the only thing
    that will ever construct a real exec ``OpContext`` and call this
    function, is not built) — there is no existing behaviour to stay
    compatible with, so fail-open buys nothing and only weakens a
    security gate for free. A missing resolver now DENIES.

    #6007 BLOCKING (architect co-vet, issuecomment-5580912677): the
    threat scan runs FIRST, unconditionally — the SAME scan
    :func:`_check_segment` applies to a segment's own argv, applied here
    to *redirect*'s own ``path`` (a surface argv-mode's whole-op scan
    covers but this module's earlier version never touched at all). Runs
    before the resolver-presence check on purpose: scanning does not
    need a ``permission_resolver`` to be wired, so a redirect with no
    resolver still gets scanned before it gets denied for the separate
    (missing-resolver) reason."""
    await _run_threat_scan(ctx, redirect.path, subject=[redirect.path])

    if ctx.permission_resolver is None:
        raise PermissionError(
            f"redirect {redirect.op!r} {redirect.path!r} cannot be checked "
            "— no permission_resolver is available on this context, and a "
            "file-axis check that cannot be consulted must not default to "
            "a grant."
        )

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
