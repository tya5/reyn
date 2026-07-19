"""Tier 2: OS invariant — #3089 index_update / index_drop / mcp_drop_server /
mcp_install thread ``bus=ctx.intervention_bus`` through their config-write
``require_file_write`` gate calls, mirroring the #3086 fix already applied to
skill_install / pipeline_install / presentation_install.

Root cause (same defect class as #3086): ``require_file_write`` only fires its
JIT interactive prompt (#1505) when ``bus is not None`` — ``bus=None`` hard-
denies outside the effective write zone with NO prompt (documented
non-interactive behaviour). The four handlers audited here built the gate's
``sandbox_policy=`` kwarg but omitted ``bus=ctx.intervention_bus``, so even
when a real ``RequestBus`` was available on ``ctx`` these config writes could
never be interactively approved once denied.

Per-entry reachability (architect audit, #3089 issue comment — bounded,
per-entry, #3037):

  - **index_update / index_drop / mcp_drop_server** (FIX — reachable): their
    tool wrappers (``tools/index_update.py`` / ``tools/drop_source.py`` /
    ``tools/mcp_drop.py``) all prefer ``ctx.router_state.op_context_factory()``
    when bound. That factory resolves to ``RouterHostAdapter.make_router_op_
    context`` in the standard chat/phase host, which threads BOTH a real
    (operator-narrowable via ``reyn.yaml``'s ``sandbox.policy.write_paths``)
    ``default_sandbox_policy`` AND a real ``intervention_bus`` inline
    (``router_host_adapter.py::make_router_op_context``, "RouterHostAdapter
    wires intervention_bus inline" per ``router_op_context.py`` module
    docstring). Their config write targets (``.reyn/config/index/sources.yaml``
    / ``.reyn/config/mcp.yaml``) also live under the ``.reyn/config/``
    recovery-core carve-out (``_RECOVERY_CORE_WRITE_PREFIXES`` —
    ``permissions.py``), which is EXCLUDED from the broad ``.reyn/`` default
    write zone — so a decl that hasn't explicitly declared/session-approved
    the exact path denies at the AgentLayer stage alone, independent of any
    sandbox narrowing. bus= is the only path back to a live decision instead
    of a silent hard-deny.
  - **mcp_install** (FIX — reachable, evidenced narrower than the other
    three): unlike its 3 siblings, ``tools/mcp_install.py::_handle_mcp_
    install_op`` never calls ``ctx.router_state.op_context_factory()`` — it
    always builds a minimal ``OpContext`` that (today) never threads
    ``default_sandbox_policy``. The fix mirrors the ``require_http_get`` call
    in the SAME function (``op_runtime/mcp_install.py``, a few lines below),
    which already threads ``ctx.intervention_bus`` unconditionally — and the
    CLI ``mcp install --source`` entry point (``interfaces/cli/commands/
    mcp.py``) already constructs a REAL ``StdinInterventionBus`` on its
    ``OpContext`` for this exact ``handle()`` call. Reachability is proven
    the same way the other three ops' handler-level tests prove it: a real
    ``OpContext`` with a narrowed ``default_sandbox_policy`` + a real bus,
    dispatched straight at ``op_runtime.mcp_install.handle`` (bypassing which
    upstream tool wrapper built it — the require_file_write call itself does
    not care who built its ctx, only what is ON it).

Real ``PermissionResolver`` + a real-``RequestBus``-compatible Fake that
pre-answers a scripted choice (mirrors ``test_config_write_jit_bus_3086.py``'s
``_FakeBus`` / ``test_require_file_jit_ask_1505.py``) — no mocks.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from reyn.core.events.events import EventLog
from reyn.core.op_runtime.context import OpContext
from reyn.data.workspace.workspace import Workspace
from reyn.intervention_choices import NO, YES
from reyn.schemas.models import (
    IndexDropIROp,
    IndexUpdateIROp,
    MCPDropServerIROp,
    MCPInstallIROp,
)
from reyn.security.permissions.permissions import PermissionDecl, PermissionResolver
from reyn.user_intervention import InterventionAnswer, UserIntervention


class _FakeBus:
    """Real RequestBus-compatible Fake that pre-answers with a scripted
    choice — implements the real ``request`` surface, not a mock (mirrors
    ``test_config_write_jit_bus_3086.py``'s ``_FakeBus``)."""

    def __init__(self, choice: str) -> None:
        self._choice = choice
        self.asks: list[UserIntervention] = []

    async def request(self, iv: UserIntervention) -> InterventionAnswer:
        self.asks.append(iv)
        return InterventionAnswer(text=self._choice, choice_id=self._choice)


def _make_ctx(
    tmp_path: Path, *, bus: object | None, write_paths: list[str],
) -> OpContext:
    """A real OpContext whose ``default_sandbox_policy.write_paths`` excludes
    the config write target (``.reyn/config/...``) — narrowing the gate
    OUTSIDE the effective write zone (SandboxLayer ∩ AgentLayer), same shape
    as ``test_config_write_jit_bus_3086.py``'s ``_make_ctx``. ``decl`` is a
    bare ``PermissionDecl()`` (nothing declared/approved), so the AgentLayer's
    OWN decl-less recovery-core-carve-out denial already reaches the JIT-ask
    branch — the sandbox narrowing is an evidenced COMPOUNDING reachability
    factor (an operator-set ``sandbox.policy.write_paths``), not the only one.
    """
    project_root = tmp_path / "proj"
    project_root.mkdir(parents=True, exist_ok=True)
    events = EventLog()
    workspace = Workspace(events=events, base_dir=project_root)
    resolver = PermissionResolver(
        config_permissions={}, project_root=project_root, interactive=True,
    )
    decl = PermissionDecl()  # no declared grants — zone/sandbox conjunction decides
    return OpContext(
        workspace=workspace,
        events=events,
        permission_decl=decl,
        permission_resolver=resolver,
        actor="test",
        intervention_bus=bus,
        default_sandbox_policy={"write_paths": write_paths},
    )


# ── 1. index_update ─────────────────────────────────────────────────────────


def _index_update_write_paths(tmp_path: Path) -> list[str]:
    # #2856 Part B: SqliteIndexBackend.drop/write/delete AND SourceManifest's
    # own atomic-write ALSO self-gate against sandbox_write_paths at the real
    # write site (independent of the require_file_write gate under test) — so
    # a write_paths cap that excludes the cache/manifest dir would deny there
    # too, masking whether THIS gate's bus= fix is what let the op through.
    # The whole project root is in scope here (SandboxLayer ⊤ for this
    # write): the JIT-ask under test fires from the AgentLayer's OWN
    # decl-less recovery-core-carve-out denial (``.reyn/config/`` is NOT the
    # default write zone and nothing here declares/approves it), independent
    # of any sandbox narrowing — mirrors the compounding-not-sole-cause note
    # in the module docstring.
    return [str(tmp_path / "proj")]


@pytest.mark.asyncio
async def test_index_update_sources_yaml_bus_none_denies(tmp_path):
    """Tier 2: #3089 — non-interactive baseline unchanged: bus=None + the
    sources.yaml config-write gate outside the effective zone → PermissionError,
    no config written."""
    from reyn.core.op_runtime.index_update import handle

    ctx = _make_ctx(tmp_path, bus=None, write_paths=_index_update_write_paths(tmp_path))
    op = IndexUpdateIROp(kind="index_update", source="mysrc", chunks=[])

    with pytest.raises(PermissionError):
        await handle(op, ctx)

    sources_yaml = ctx.workspace.base_dir / ".reyn" / "config" / "index" / "sources.yaml"
    assert not sources_yaml.exists()


@pytest.mark.asyncio
async def test_index_update_sources_yaml_bus_approves(tmp_path):
    """Tier 2: #3089 fix — a real bus threaded through the gate lets the
    operator interactively approve the narrowed sources.yaml write; the op
    succeeds and the JIT prompt fired exactly once. RED if bus= is not passed
    through to require_file_write (the #3089 regression)."""
    from reyn.core.op_runtime.index_update import handle

    bus = _FakeBus(YES)
    ctx = _make_ctx(tmp_path, bus=bus, write_paths=_index_update_write_paths(tmp_path))
    op = IndexUpdateIROp(kind="index_update", source="mysrc", chunks=[])

    result = await handle(op, ctx)

    assert "error" not in result or result.get("error") is None, f"update failed: {result}"
    (ask,) = bus.asks  # exactly one prompt fired
    assert "sources.yaml" in ask.prompt or "sources.yaml" in ask.detail
    sources_yaml = ctx.workspace.base_dir / ".reyn" / "config" / "index" / "sources.yaml"
    assert "mysrc" in sources_yaml.read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_index_update_sources_yaml_bus_denies_after_prompt(tmp_path):
    """Tier 2: #3089 — the operator can DENY via the prompt; PermissionError
    still raised, but only after a prompt fired."""
    from reyn.core.op_runtime.index_update import handle

    bus = _FakeBus(NO)
    ctx = _make_ctx(tmp_path, bus=bus, write_paths=_index_update_write_paths(tmp_path))
    op = IndexUpdateIROp(kind="index_update", source="mysrc", chunks=[])

    with pytest.raises(PermissionError):
        await handle(op, ctx)

    (ask,) = bus.asks
    assert "sources.yaml" in ask.prompt or "sources.yaml" in ask.detail


# ── 2. index_drop ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_index_drop_sources_yaml_bus_none_denies(tmp_path):
    """Tier 2: #3089 — non-interactive baseline unchanged for index_drop."""
    from reyn.core.op_runtime.index_drop import handle

    ctx = _make_ctx(tmp_path, bus=None, write_paths=_index_update_write_paths(tmp_path))
    op = IndexDropIROp(kind="index_drop", source="mysrc")

    with pytest.raises(PermissionError):
        await handle(op, ctx)


@pytest.mark.asyncio
async def test_index_drop_sources_yaml_bus_approves(tmp_path):
    """Tier 2: #3089 fix — index_drop threads bus= through the sources.yaml
    gate; a narrowed write is interactively approvable and the drop completes."""
    from reyn.core.op_runtime.index_drop import handle

    bus = _FakeBus(YES)
    ctx = _make_ctx(tmp_path, bus=bus, write_paths=_index_update_write_paths(tmp_path))
    op = IndexDropIROp(kind="index_drop", source="mysrc")

    result = await handle(op, ctx)

    assert result["chunks_dropped"] == 0  # nothing was ever indexed — a clean no-op drop
    (ask,) = bus.asks  # exactly one prompt fired (source_dir write is in-zone, no ask needed)
    assert "sources.yaml" in ask.prompt or "sources.yaml" in ask.detail


@pytest.mark.asyncio
async def test_index_drop_sources_yaml_bus_denies_after_prompt(tmp_path):
    """Tier 2: #3089 — index_drop operator denial via prompt."""
    from reyn.core.op_runtime.index_drop import handle

    bus = _FakeBus(NO)
    ctx = _make_ctx(tmp_path, bus=bus, write_paths=_index_update_write_paths(tmp_path))
    op = IndexDropIROp(kind="index_drop", source="mysrc")

    with pytest.raises(PermissionError):
        await handle(op, ctx)

    (ask,) = bus.asks
    assert "sources.yaml" in ask.prompt or "sources.yaml" in ask.detail


# ── 3. mcp_drop_server ───────────────────────────────────────────────────────


def _seed_mcp_yaml(tmp_path: Path, server: str) -> Path:
    config_path = tmp_path / "proj" / ".reyn" / "config" / "mcp.yaml"
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(
        f"mcp:\n  servers:\n    {server}:\n      type: stdio\n      command: /bin/cat\n",
        encoding="utf-8",
    )
    return config_path


@pytest.mark.asyncio
async def test_mcp_drop_server_config_write_bus_none_denies(tmp_path):
    """Tier 2: #3089 — non-interactive baseline unchanged for mcp_drop_server."""
    from reyn.core.op_runtime.mcp_drop_server import handle

    _seed_mcp_yaml(tmp_path, "planted")
    unrelated = tmp_path / "unrelated"
    unrelated.mkdir(parents=True, exist_ok=True)
    ctx = _make_ctx(tmp_path, bus=None, write_paths=[str(unrelated)])
    op = MCPDropServerIROp(kind="mcp_drop_server", server="planted")

    with pytest.raises(PermissionError):
        await handle(op, ctx)

    config_path = ctx.workspace.base_dir / ".reyn" / "config" / "mcp.yaml"
    assert "planted" in config_path.read_text(encoding="utf-8")  # untouched


@pytest.mark.asyncio
async def test_mcp_drop_server_config_write_bus_approves(tmp_path):
    """Tier 2: #3089 fix — mcp_drop_server threads bus= through the mcp.yaml
    gate; the narrowed write is interactively approvable and the drop
    completes. RED if bus= is not passed through (the #3089 regression)."""
    from reyn.core.op_runtime.mcp_drop_server import handle

    _seed_mcp_yaml(tmp_path, "planted")
    unrelated = tmp_path / "unrelated"
    unrelated.mkdir(parents=True, exist_ok=True)
    bus = _FakeBus(YES)
    ctx = _make_ctx(tmp_path, bus=bus, write_paths=[str(unrelated)])
    op = MCPDropServerIROp(kind="mcp_drop_server", server="planted")

    result = await handle(op, ctx)

    assert result["status"] == "ok", f"drop failed: {result}"
    (ask,) = bus.asks
    assert "mcp.yaml" in ask.prompt or "mcp.yaml" in ask.detail
    config_path = ctx.workspace.base_dir / ".reyn" / "config" / "mcp.yaml"
    assert "planted" not in config_path.read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_mcp_drop_server_config_write_bus_denies_after_prompt(tmp_path):
    """Tier 2: #3089 — mcp_drop_server operator denial via prompt."""
    from reyn.core.op_runtime.mcp_drop_server import handle

    _seed_mcp_yaml(tmp_path, "planted")
    unrelated = tmp_path / "unrelated"
    unrelated.mkdir(parents=True, exist_ok=True)
    bus = _FakeBus(NO)
    ctx = _make_ctx(tmp_path, bus=bus, write_paths=[str(unrelated)])
    op = MCPDropServerIROp(kind="mcp_drop_server", server="planted")

    with pytest.raises(PermissionError):
        await handle(op, ctx)

    (ask,) = bus.asks
    assert "mcp.yaml" in ask.prompt or "mcp.yaml" in ask.detail


# ── 4. mcp_install ───────────────────────────────────────────────────────────

# npm: --source skips the registry fetch (op.source is set) entirely — the
# only remaining external dependency is the npx binary on PATH, which is
# stubbed via ``shutil.which`` (same technique test_mcp_source_install.py
# uses for the identical source-install path) so the test needs no network.
_NPM_SOURCE = "npm:@example-org/example-mcp-server"


@pytest.mark.asyncio
async def test_mcp_install_config_write_bus_none_denies(tmp_path, monkeypatch):
    """Tier 2: #3089 — non-interactive baseline unchanged for mcp_install."""
    from reyn.core.op_runtime.mcp_install import handle

    monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/npx")
    unrelated = tmp_path / "unrelated"
    unrelated.mkdir(parents=True, exist_ok=True)
    ctx = _make_ctx(tmp_path, bus=None, write_paths=[str(unrelated)])
    op = MCPInstallIROp(kind="mcp_install", server_id="", source=_NPM_SOURCE)

    with pytest.raises(PermissionError):
        await handle(op, ctx)

    config_path = ctx.workspace.base_dir / ".reyn" / "config" / "mcp.yaml"
    assert not config_path.exists()


@pytest.mark.asyncio
async def test_mcp_install_config_write_bus_approves(tmp_path, monkeypatch):
    """Tier 2: #3089 fix — mcp_install threads bus= through the mcp.yaml
    gate (mirrors its own require_http_get call a few lines below, which
    already does this); the narrowed write is interactively approvable and
    the install completes. RED if bus= is not passed through to
    require_file_write (the #3089 regression)."""
    from reyn.core.op_runtime.mcp_install import handle

    monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/npx")
    unrelated = tmp_path / "unrelated"
    unrelated.mkdir(parents=True, exist_ok=True)
    bus = _FakeBus(YES)
    ctx = _make_ctx(tmp_path, bus=bus, write_paths=[str(unrelated)])
    op = MCPInstallIROp(kind="mcp_install", server_id="", source=_NPM_SOURCE)

    result = await handle(op, ctx)

    assert result["status"] == "ok", f"install failed: {result}"
    (ask,) = bus.asks
    assert "mcp.yaml" in ask.prompt or "mcp.yaml" in ask.detail
    config_path = ctx.workspace.base_dir / ".reyn" / "config" / "mcp.yaml"
    assert "example-mcp-server" in config_path.read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_mcp_install_config_write_bus_denies_after_prompt(tmp_path, monkeypatch):
    """Tier 2: #3089 — mcp_install operator denial via prompt."""
    from reyn.core.op_runtime.mcp_install import handle

    monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/npx")
    unrelated = tmp_path / "unrelated"
    unrelated.mkdir(parents=True, exist_ok=True)
    bus = _FakeBus(NO)
    ctx = _make_ctx(tmp_path, bus=bus, write_paths=[str(unrelated)])
    op = MCPInstallIROp(kind="mcp_install", server_id="", source=_NPM_SOURCE)

    with pytest.raises(PermissionError):
        await handle(op, ctx)

    (ask,) = bus.asks
    assert "mcp.yaml" in ask.prompt or "mcp.yaml" in ask.detail


# ── 5. strip-falsify — the defect class in isolation ───────────────────────


@pytest.mark.asyncio
async def test_strip_falsify_omitting_bus_reproduces_original_bug(tmp_path):
    """Tier 2: #3089 — strip-falsify at the PermissionResolver level, proving
    the defect class the production fix closes: a config-write gate call that
    OMITS ``bus=`` (the exact pre-fix shape of index_update.py / index_drop.py
    / mcp_drop_server.py / mcp_install.py's require_file_write calls) denies
    even when a bus that WOULD approve is sitting right there on the context.
    Passing that same bus through (the fix) succeeds. RED if
    require_file_write's bus=None default ever stops mattering (i.e. if
    outside-zone writes start being silently granted regardless of bus)."""
    project_root = tmp_path / "proj"
    project_root.mkdir(parents=True, exist_ok=True)
    unrelated_write_dir = tmp_path / "unrelated"
    unrelated_write_dir.mkdir(parents=True, exist_ok=True)
    config_path = project_root / ".reyn" / "config" / "mcp.yaml"

    resolver = PermissionResolver(
        config_permissions={}, project_root=project_root, interactive=True,
    )
    decl = PermissionDecl()
    from reyn.security.sandbox.policy import SandboxPolicy

    sandbox = SandboxPolicy(write_paths=[str(unrelated_write_dir)])
    bus_that_would_approve = _FakeBus(YES)

    # Pre-fix shape: bus= omitted (mirrors all 4 handlers' bug verbatim).
    with pytest.raises(PermissionError):
        await resolver.require_file_write(
            decl, str(config_path), "test", sandbox_policy=sandbox,
        )
    assert not bus_that_would_approve.asks, "a bus that was never passed cannot have been consulted"

    # Fixed shape: bus= threaded through — same resolver, same sandbox, same path.
    await resolver.require_file_write(
        decl, str(config_path), "test", sandbox_policy=sandbox, bus=bus_that_would_approve,
    )
    (ask,) = bus_that_would_approve.asks  # exactly one prompt fired
    assert "mcp.yaml" in ask.prompt or "mcp.yaml" in ask.detail
