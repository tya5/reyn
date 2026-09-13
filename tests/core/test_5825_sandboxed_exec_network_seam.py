"""Tier 2: run_sandboxed_exec's own #5825① network-request seam (architect
ruling, issue #5825, 2026-09-06).

Real ``OpContext`` + real ``PermissionResolver`` + a real-``RequestBus``-
compatible Fake (mirrors ``test_3089_op_runtime_require_file_write_bus.py``'s
own ``_FakeBus``) + a real ``SandboxBackend`` test double (NOT a mock — mirrors
``test_sandbox_denial_class_5244.py``'s own ``_NetworkDenyingBackend``) that
always succeeds, so these tests exercise the PERMISSION seam (ask fires / is
skipped, the call's policy is replaced on grant) without depending on any
platform's real OS-level network enforcement (Seatbelt/Landlock/Docker) — that
enforcement itself is out of scope here; ``test_sandbox_denial_class_5244.py``
covers the classification side once a real denial occurs.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from reyn.core.events.events import EventLog
from reyn.core.op_runtime.context import OpContext
from reyn.core.op_runtime.sandboxed_exec import run_sandboxed_exec
from reyn.data.workspace.workspace import Workspace
from reyn.intervention_choices import NO, YES
from reyn.schemas.models import SandboxedExecIROp
from reyn.security.permissions.permissions import PermissionDecl, PermissionResolver
from reyn.security.sandbox.backend import SandboxResult
from reyn.user_intervention import InterventionAnswer, UserIntervention
from tests._support.events import collect_events, settle
from tests._support.sandbox_backend import FULLY_ENFORCING_AXES


class _FakeBus:
    """Real RequestBus-compatible fake that pre-answers with a scripted
    choice — implements the real ``request`` surface, not a mock."""

    def __init__(self, choice: str) -> None:
        self._choice = choice
        self.asks: list[UserIntervention] = []

    async def request(self, iv: UserIntervention) -> InterventionAnswer:
        self.asks.append(iv)
        return InterventionAnswer(text=self._choice, choice_id=self._choice)


class _SuccessBackend:
    """Real SandboxBackend test double (NOT a mock) that always succeeds —
    mirrors ``test_sandbox_denial_class_5244.py``'s own
    ``_NetworkDenyingBackend``, the accept-side sibling."""

    name = "fake-success"
    enforced_axes = FULLY_ENFORCING_AXES

    def available(self) -> bool:
        return True

    def wrap_command(self, argv, policy):  # pragma: no cover - unused here
        from reyn.security.sandbox.backend import WrappedCommand

        return WrappedCommand(argv=list(argv), env={})

    async def run(
        self, argv, policy, *, stdin=None, cwd=None, cancel_event=None,
        hook_process_context=None, sink=None, env_path=None,
    ):
        return SandboxResult(returncode=0, stdout=b"ok", stderr=b"")


def _make_ctx(
    tmp_path: Path, *, bus, network_policy: bool, config: dict | None = None,
) -> tuple[OpContext, list]:
    project_root = tmp_path / "proj"
    project_root.mkdir(parents=True, exist_ok=True)
    events = EventLog()
    collected = collect_events(events)
    workspace = Workspace(events=events, base_dir=project_root)
    resolver = PermissionResolver(
        config_permissions=config or {}, project_root=project_root, interactive=True,
    )
    ctx = OpContext(
        workspace=workspace,
        events=events,
        permission_decl=PermissionDecl(),
        permission_resolver=resolver,
        actor="test",
        intervention_bus=bus,
        default_sandbox_policy={"network": network_policy},
        sandbox_backend=_SuccessBackend(),
    )
    return ctx, collected


def _started_events(collected: list) -> list:
    return [e for e in collected if e.type == "sandboxed_exec_started"]


@pytest.mark.asyncio
async def test_policy_already_open_with_no_config_runs_without_asking(tmp_path: Path) -> None:
    """Tier 2: policy.network already True (compat/unbounded), no
    ``permissions.network`` config either way -- runs without asking,
    regardless of the op's own request.

    #5825 §3 fix (renamed from ``test_policy_already_open_skips_require_
    network``, which is now inaccurate): ``require_network`` IS called for
    every ``op.network is True`` request as of #5825 §3 (see that method's
    own ``policy_already_open`` parameter) -- what stays true, and what
    this test actually asserts, is the OBSERVABLE behavior: no ask, no
    sandboxed_exec_started change. See ``test_config_network_deny_blocks_
    an_already_open_policy`` below for the accept-side witness that the
    call now genuinely happens (a configured deny DOES fire here, where
    it silently did not before #5825 §3)."""
    bus = _FakeBus(NO)  # would deny if ever asked -- proves it was never asked
    ctx, collected = _make_ctx(tmp_path, bus=bus, network_policy=True)
    op = SandboxedExecIROp(kind="sandboxed_exec", argv=["true"], network=True)

    result = await run_sandboxed_exec(op, ctx)

    assert result["status"] == "ok"
    assert bus.asks == []
    await settle(ctx.events)
    (started,) = _started_events(collected)
    assert started.data["network"] is True


@pytest.mark.asyncio
async def test_config_network_deny_blocks_an_already_open_policy(tmp_path: Path) -> None:
    """Tier 2: #5825 §3 -- the accept-side witness for the security gap
    the census found. ``permissions.network: deny`` (the operator's own
    floor) must block a run even when the resolved sandbox policy ALREADY
    has network on (``network_policy=True`` -- compat/``unbounded``).

    Before #5825 §3: the call site's own guard (`if op.network and not
    policy.network:`) skipped ``require_network`` entirely whenever
    ``policy.network`` was already ``True`` -- the floor inside
    ``require_network`` (checked first, unconditionally, in that method's
    own body) was therefore NEVER REACHED, and a configured
    ``permissions.network: deny`` silently did nothing under
    compat/``unbounded``.

    Strip-falsifier (performed during review): reverting the call site's
    guard to ``if op.network and not policy.network:`` (dropping the
    unconditional call) makes this test fail -- the run succeeds instead
    of raising, because ``require_network`` (and its floor check) is
    never reached."""
    bus = _FakeBus(YES)  # would grant if ever asked -- proves the floor denies first
    ctx, collected = _make_ctx(
        tmp_path, bus=bus, network_policy=True, config={"network": "deny"},
    )
    op = SandboxedExecIROp(kind="sandboxed_exec", argv=["true"], network=True)

    with pytest.raises(PermissionError, match="denied by config"):
        await run_sandboxed_exec(op, ctx)

    assert bus.asks == []
    await settle(ctx.events)
    assert collected == []


@pytest.mark.asyncio
async def test_op_requests_network_against_closed_policy_asks_and_grants(
    tmp_path: Path,
) -> None:
    """Tier 2: op.network=True against a CLOSED policy asks exactly once;
    YES grants -- the CALL's policy is replaced with network=True, and
    sandboxed_exec_started records the GRANTED (enforced) value, not the
    op's own unvalidated request field (#1339's own rule)."""
    bus = _FakeBus(YES)
    ctx, collected = _make_ctx(tmp_path, bus=bus, network_policy=False)
    op = SandboxedExecIROp(kind="sandboxed_exec", argv=["true"], network=True)

    result = await run_sandboxed_exec(op, ctx)

    assert result["status"] == "ok"
    (_ask,) = bus.asks  # exactly one — tuple-unpack raises on any other count
    await settle(ctx.events)
    (started,) = _started_events(collected)
    assert started.data["network"] is True


@pytest.mark.asyncio
async def test_op_requests_network_against_closed_policy_denies_before_spawn(
    tmp_path: Path,
) -> None:
    """Tier 2: NO denies with a PermissionError raised BEFORE the process
    spawns -- no sandboxed_exec_started/completed event is ever emitted (the
    seam runs strictly before backend.run, per the seam's own placement)."""
    bus = _FakeBus(NO)
    ctx, collected = _make_ctx(tmp_path, bus=bus, network_policy=False)
    op = SandboxedExecIROp(kind="sandboxed_exec", argv=["true"], network=True)

    with pytest.raises(PermissionError):
        await run_sandboxed_exec(op, ctx)

    (_ask,) = bus.asks  # exactly one — tuple-unpack raises on any other count
    await settle(ctx.events)
    assert collected == []


@pytest.mark.asyncio
async def test_op_omits_network_runs_closed_without_asking(tmp_path: Path) -> None:
    """Tier 2: op.network=False (the default -- an omitted request) against a
    closed policy never calls require_network -- 0 asks, runs closed, exactly
    the present sibling of the "asks and grants" case above."""
    bus = _FakeBus(YES)  # would grant if ever asked -- proves it was never asked
    ctx, collected = _make_ctx(tmp_path, bus=bus, network_policy=False)
    op = SandboxedExecIROp(kind="sandboxed_exec", argv=["true"])  # network omitted

    result = await run_sandboxed_exec(op, ctx)

    assert result["status"] == "ok"
    assert bus.asks == []
    await settle(ctx.events)
    (started,) = _started_events(collected)
    assert started.data["network"] is False


@pytest.mark.asyncio
async def test_config_network_deny_blocks_the_request_before_spawn(
    tmp_path: Path,
) -> None:
    """Tier 2: permissions.network: deny (the floor) blocks a requested,
    otherwise-closed run without ever asking -- no started/completed event."""
    bus = _FakeBus(YES)  # would grant if ever asked -- proves it was never asked
    ctx, collected = _make_ctx(
        tmp_path, bus=bus, network_policy=False, config={"network": "deny"},
    )
    op = SandboxedExecIROp(kind="sandboxed_exec", argv=["true"], network=True)

    with pytest.raises(PermissionError, match="denied by config"):
        await run_sandboxed_exec(op, ctx)

    assert bus.asks == []
    await settle(ctx.events)
    assert collected == []


@pytest.mark.asyncio
async def test_no_permission_resolver_denies_a_network_request_fail_closed(
    tmp_path: Path,
) -> None:
    """Tier 2: a requested, otherwise-closed run with NO permission_resolver
    on the ctx (a narrower/test-shaped context) denies rather than silently
    opening -- the gate that cannot be consulted must not default to a
    grant on a security-sensitive axis (the same posture the module's own
    seam comment states)."""
    project_root = tmp_path / "proj"
    project_root.mkdir(parents=True, exist_ok=True)
    events = EventLog()
    collected = collect_events(events)
    workspace = Workspace(events=events, base_dir=project_root)
    ctx = OpContext(
        workspace=workspace,
        events=events,
        permission_decl=PermissionDecl(),
        permission_resolver=None,
        default_sandbox_policy={"network": False},
        sandbox_backend=_SuccessBackend(),
    )
    op = SandboxedExecIROp(kind="sandboxed_exec", argv=["true"], network=True)

    with pytest.raises(PermissionError, match="no permission resolver"):
        await run_sandboxed_exec(op, ctx)

    await settle(ctx.events)
    assert collected == []


@pytest.mark.asyncio
async def test_no_permission_resolver_denies_even_against_an_already_open_policy(
    tmp_path: Path,
) -> None:
    """Tier 2: #5825 §3 review (lead-coder correction, architect census) —
    the SAME fail-closed posture as the sibling test above, but with
    ``policy.network`` already ``True`` (compat/``unbounded``-shaped).

    Before this correction, a resolver-less context with an already-open
    policy would have silently skipped the whole gate (no raise) — "no
    resolver, so the config deny cannot be checked" must never be read as
    "no deny exists"; that is this issue's own defect shape re-entering
    through a different door. The function's own pre-existing behavior
    (raise when no resolver, see the sibling test) is what this case now
    matches too, unconditionally of ``policy.network``."""
    project_root = tmp_path / "proj"
    project_root.mkdir(parents=True, exist_ok=True)
    events = EventLog()
    collected = collect_events(events)
    workspace = Workspace(events=events, base_dir=project_root)
    ctx = OpContext(
        workspace=workspace,
        events=events,
        permission_decl=PermissionDecl(),
        permission_resolver=None,
        default_sandbox_policy={"network": True},  # already open — compat/unbounded
        sandbox_backend=_SuccessBackend(),
    )
    op = SandboxedExecIROp(kind="sandboxed_exec", argv=["true"], network=True)

    with pytest.raises(PermissionError, match="no permission resolver"):
        await run_sandboxed_exec(op, ctx)

    await settle(ctx.events)
    assert collected == []
