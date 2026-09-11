"""Tier 2: OS invariant — #5825 stage 2 (architect design + lead-coder
dispatch, doc §6/§6.1/§8): ``permissions.mode: bounded`` SELECTS the
existing ``sandbox.mode: strict`` preset, and the resolved posture
DOWNGRADES to ``ask`` when the resolved sandbox boundary cannot actually
enforce a closed network — never silently recorded as ``bounded`` with no
real boundary behind it (doc §6's own prohibition), and never a hard
refusal to start either (architect's own derivation: ``bounded`` is a
TRADE, not a strictness notch — void the trade, don't punish the operator
who asked for the safer mode).

Builds on ``tests/runtime/test_5825_8_network_enforcement_gap.py``'s own
established fixtures (a REAL ``Session`` via ``make_session``, a REAL
``NoopBackend`` injected for "genuinely cannot enforce" — never a mock).
That file's own 6 tests keep covering ``network_enforcement_gap`` in
isolation; this file covers what #5825 stage 2 added ON TOP of it:
``Session.configured_permission_mode`` / ``resolved_permission_mode`` /
``permission_mode_downgrade_reason``, and the real enforcement-path
wiring (``router_op_context.build_router_op_context`` selecting
``sandbox.mode: strict`` from ``bounded`` — the SAME
``sandbox_mode_for_permission_mode`` call ``network_enforcement_gap``
itself uses, never a second, independently-computed decision).

``tests/security/test_5825_permission_posture_dial.py`` owns the pure
vocabulary layer (``PermissionMode``, parsing, ordering,
``sandbox_mode_for_permission_mode`` as a bare function) — this file is
the session-level judgment layer built on top of it.
"""
from __future__ import annotations

from pathlib import Path

from reyn.config.infra import SandboxConfig
from reyn.security.permissions.permissions import PermissionResolver
from reyn.security.permissions.posture import PermissionMode
from reyn.security.sandbox.noop_backend import NoopBackend
from tests._support.agent_session import make_session


def _bounded_session(
    tmp_path: Path,
    *,
    sandbox_mode: str = "compat",
    disable_unbounded_mode: bool = False,
):
    """A REAL Session (no fakes) with a REAL ``PermissionResolver``
    configured for ``permissions.mode: bounded``, and a REAL
    ``NoopBackend`` injected as its sandbox backend (same "genuinely
    cannot enforce" fixture ``test_5825_8_network_enforcement_gap.py``
    already established). ``sandbox_mode="compat"`` by default —
    DELIBERATELY the opposite of what ``bounded`` needs, so a test using
    this default is proving `bounded` OVERRIDES the operator's own
    ``sandbox.mode``, not merely agreeing with it."""
    config = {"mode": "bounded"}
    if disable_unbounded_mode:
        config["disable_unbounded_mode"] = True
    return make_session(
        agent_name="bounded-stage2-test",
        workspace_base_dir=tmp_path,
        workspace_state_dir=tmp_path / ".reyn",
        permission_resolver=PermissionResolver(config, project_root=tmp_path),
        sandbox_config=SandboxConfig(mode=sandbox_mode),
        sandbox_backend=NoopBackend(),
    )


# ── configured_permission_mode / resolved_permission_mode ───────────────


def test_configured_permission_mode_reads_the_real_config(tmp_path):
    """Tier 2: the configured value is exactly what was written — no
    downgrade applied yet."""
    session = _bounded_session(tmp_path)

    assert session.configured_permission_mode is PermissionMode.BOUNDED


def test_bounded_forces_strict_regardless_of_configured_sandbox_mode(tmp_path):
    """Tier 2: the core stage-2 claim — ``bounded`` SELECTS ``sandbox.mode:
    strict`` (closed network) even though this session's OWN
    ``sandbox.mode`` is ``compat`` (open network by default). Observed
    through ``network_enforcement_gap``: a gap can only exist at all if
    the resolved policy asked for network to be closed in the first
    place (its own docstring/§8's discriminating-control test in the
    sibling file) — so a non-``None`` gap here, under a ``compat``
    ``sandbox.mode``, is only explainable by `bounded` having forced
    ``strict``.

    Strip-falsify (verified by hand, file-internal Edit only, reverted):
    removing `sandbox_mode_for_permission_mode`'s call in
    `Session.network_enforcement_gap` (reverting to the pre-#5825-stage-2
    `sandbox_config.mode if ... else "compat"` line) makes this test fail
    — the gap reads `None` because the unmodified `compat` policy never
    asks for network to be closed at all."""
    session = _bounded_session(tmp_path, sandbox_mode="compat")

    gap = session.network_enforcement_gap

    assert gap is not None, (
        "bounded did not force sandbox.mode: strict -- compat's own "
        "open-network default left nothing for NoopBackend to fail at"
    )
    assert "no isolation" in gap, gap


def test_resolved_permission_mode_downgrades_to_ask_when_unenforceable(tmp_path):
    """Tier 2: doc §8's own strip-falsifier shape -- "removing the
    boundary and watching the acceptance go red, not watching the prompt
    disappear". NoopBackend IS the "boundary removed" state (it enforces
    nothing, #4039's own declaration) -- resolved_permission_mode must
    read `ask`, not `bounded` recorded against a boundary that is not
    real (doc §6's own prohibition: "the boundary replaces the prompt";
    with no boundary, the trade is void).

    Stage 2 is DISPLAY-ONLY: this property's only reader today is the
    project_status/Ctx-pane row (status.py's `permission_mode` key) --
    no prompt-issuing consumer reads it yet, so this test asserts the
    judgment (`ask` is the correct effective value), not that a prompt
    actually fires. Wiring a prompt-issuing consumer to this value is
    stage 3's own, not-yet-built scope."""
    session = _bounded_session(tmp_path)

    assert session.resolved_permission_mode is PermissionMode.ASK
    assert session.configured_permission_mode is PermissionMode.BOUNDED, (
        "the CONFIGURED value must stay bounded -- only the EFFECTIVE "
        "(resolved) value downgrades; a config-load-time mutation would "
        "make the operator's own reyn.yaml unrecoverable without editing it"
    )


def test_permission_mode_downgrade_reason_names_the_network_gap(tmp_path):
    """Tier 2: the Ctx-pane row's own reason text is EXACTLY
    network_enforcement_gap's string -- never a second, independently-
    worded explanation (architect's own deny-side acceptance criterion:
    two judgment paths let the pane and the real decision diverge)."""
    session = _bounded_session(tmp_path)

    assert session.permission_mode_downgrade_reason == session.network_enforcement_gap
    assert session.permission_mode_downgrade_reason is not None


def test_no_downgrade_reason_when_there_is_no_gap(tmp_path):
    """Tier 2: deny side -- the discriminating control mirrors
    test_5825_8_network_enforcement_gap.py's own "an open-network policy
    reports no gap" test. `ask` never asks for a boundary in the first
    place (`sandbox_mode_for_permission_mode` passes `sandbox.mode`
    through unchanged for every mode but `bounded`), so `compat` leaves
    network open, NoopBackend's own non-enforcement has nothing to fail
    at, and neither the gap nor the downgrade reason should exist."""
    session = make_session(
        agent_name="ask-stage2-control",
        workspace_base_dir=tmp_path,
        workspace_state_dir=tmp_path / ".reyn",
        permission_resolver=PermissionResolver({"mode": "ask"}, project_root=tmp_path),
        sandbox_config=SandboxConfig(mode="compat"),
        sandbox_backend=NoopBackend(),
    )

    assert session.network_enforcement_gap is None
    assert session.resolved_permission_mode is PermissionMode.ASK
    assert session.permission_mode_downgrade_reason is None


def test_read_only_and_unbounded_are_never_touched_by_the_bounded_downgrade(tmp_path):
    """Tier 2: deny side -- the downgrade path is scoped to `bounded`
    alone (mirrors test_5825_permission_posture_dial.py's own
    `test_other_permissions_keys_keep_ordinary_last_tier_wins_merge`
    scoping-deny pattern, one layer up). Neither mode forces
    sandbox.mode: strict, so NoopBackend's own non-enforcement has
    nothing to fail at for either."""
    for mode in ("read_only", "unbounded"):
        session = make_session(
            agent_name=f"{mode}-stage2-control",
            workspace_base_dir=tmp_path,
            workspace_state_dir=tmp_path / ".reyn",
            permission_resolver=PermissionResolver({"mode": mode}, project_root=tmp_path),
            sandbox_config=SandboxConfig(mode="compat"),
            sandbox_backend=NoopBackend(),
        )
        assert session.network_enforcement_gap is None, mode
        assert session.resolved_permission_mode is PermissionMode(mode), mode
        assert session.permission_mode_downgrade_reason is None, mode


# ── the unbounded-lock downgrade rides the SAME session-level surface ───


def test_unbounded_lock_downgrade_is_visible_on_the_same_session_surface(tmp_path):
    """Tier 2: architect -- "stage 1's own weak point (the unbounded
    downgrade was visible only in reyn.log) disappears for free" once
    stage 2's session-level properties exist. `configured_permission_mode`
    returns the pre-lock value (`unbounded`, as configured -- the lock is
    a resolution-time fact, not a config-load-time mutation, same
    contract as the bounded-network downgrade above); resolved reads
    `ask`; the reason names the lock, distinctly from the network-gap
    wording the bounded case uses."""
    session = make_session(
        agent_name="unbounded-lock-stage2",
        workspace_base_dir=tmp_path,
        workspace_state_dir=tmp_path / ".reyn",
        permission_resolver=PermissionResolver(
            {"mode": "unbounded", "disable_unbounded_mode": True}, project_root=tmp_path,
        ),
    )

    assert session.configured_permission_mode is PermissionMode.UNBOUNDED
    assert session.resolved_permission_mode is PermissionMode.ASK
    assert session.permission_mode_downgrade_reason == (
        "disabled by permissions.disable_unbounded_mode"
    )


# ── the real enforcement path (router_op_context) ────────────────────────


def test_build_router_op_context_resolves_strict_for_a_bounded_session(tmp_path):
    """Tier 2: the REAL exec path, not just the observability side --
    ``build_router_op_context`` (called from Session's own router-waist
    construction) must resolve ``sandbox.mode: strict``'s own network
    deny for a ``bounded`` session, the SAME as
    ``network_enforcement_gap`` already proved it detects. Reads the
    constructed ``OpContext.default_sandbox_policy`` directly -- the real
    object every op dispatch reads for its own policy, not a
    reimplementation of the resolution."""
    from reyn.core.events.events import EventLog
    from reyn.data.workspace.workspace import Workspace
    from reyn.runtime.router_op_context import build_router_op_context

    permission_resolver = PermissionResolver({"mode": "bounded"}, project_root=tmp_path)
    events = EventLog()
    workspace = Workspace(events=events, base_dir=tmp_path)
    ctx = build_router_op_context(
        events=events,
        permission_resolver=permission_resolver,
        file_permissions=None,
        mcp_servers=None,
        mcp_servers_flat=[],
        allowed_mcp=None,
        workspace_base_dir=workspace.base_dir,
        workspace_state_dir=tmp_path / ".reyn",
        environment_backend=None,
        sandbox_backend=NoopBackend(),
        sandbox_policy=None,
        sandbox_config=SandboxConfig(mode="compat"),
        agent_id="bounded-router-op-context-test",
        presentation_renderer=None,
    )

    assert ctx.default_sandbox_policy is not None
    assert ctx.default_sandbox_policy.get("network") is False, (
        "a bounded session's real exec-context policy left network open "
        "-- compat's own sandbox.mode was used instead of bounded's own "
        "strict selection"
    )


def test_build_router_op_context_leaves_ask_at_the_configured_sandbox_mode(tmp_path):
    """Tier 2: deny side -- the SAME construction with `ask` (not
    `bounded`) must NOT force strict; `compat`'s own open-network default
    stays."""
    from reyn.core.events.events import EventLog
    from reyn.data.workspace.workspace import Workspace
    from reyn.runtime.router_op_context import build_router_op_context

    permission_resolver = PermissionResolver({"mode": "ask"}, project_root=tmp_path)
    events = EventLog()
    workspace = Workspace(events=events, base_dir=tmp_path)
    ctx = build_router_op_context(
        events=events,
        permission_resolver=permission_resolver,
        file_permissions=None,
        mcp_servers=None,
        mcp_servers_flat=[],
        allowed_mcp=None,
        workspace_base_dir=workspace.base_dir,
        workspace_state_dir=tmp_path / ".reyn",
        environment_backend=None,
        sandbox_backend=NoopBackend(),
        sandbox_policy=None,
        sandbox_config=SandboxConfig(mode="compat"),
        agent_id="ask-router-op-context-control",
        presentation_renderer=None,
    )

    assert ctx.default_sandbox_policy is not None
    assert ctx.default_sandbox_policy.get("network") is True
