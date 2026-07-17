"""Tier 2: ``mcp__install_local`` must honour the recovery-core write-gate contract.

``.reyn/config/`` is a recovery-core write-gate prefix (``_RECOVERY_CORE_WRITE_PREFIXES``,
#2248 PR-C): a write there must be explicitly GATED (never silently allowed by the broad
``.reyn/`` default zone) and must contribute a truncation-surviving config GENERATION,
because the directory is reconstructed from those generations.

``mcp__install_local`` writes ``.reyn/config/mcp.yaml`` directly — bypassing the mcp_install
op — and honoured neither half:

* Its permission gate read ``getattr(rs, "permission_resolver", None)`` — a field
  ``RouterCallerState`` has never declared, so ``getattr``'s default swallowed the miss and
  the gate never fired in ANY context. An LLM could write the operator's MCP config with no
  decision anywhere in the chain, which ``reyn pipe run``'s trusted-by-configuration
  auto-grant (#2932) then turned into a live grant.
* It recorded no config generation, so ``_reconcile_config_as_of_cut`` — which rewrites every
  generation-tracked registry from its LATEST generation — silently ERASED its servers on any
  rewind / crash recovery once a generation existed for ``config/mcp.yaml``.
* It emitted no P6 audit-event on success, so the install left no trace to inspect.

The three are tested together because they are one contract: gating the write without
recording its generation would keep the recovery hole; recording without gating would keep
the escalation.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest
import yaml

from reyn.core.events.snapshot_generations import rewind
from reyn.core.events.state_log import StateLog
from reyn.runtime.registry import AgentRegistry
from reyn.security.permissions.permissions import PermissionDecl, PermissionResolver
from reyn.tools.mcp_verbs import MCP_INSTALL_LOCAL
from reyn.tools.types import RouterCallerState, ToolContext
from reyn.user_intervention import InterventionAnswer


class _RecordingBus:
    """Real RequestBus-shaped collaborator (non-mock): records asks, answers a
    pre-scripted choice. Mirrors the ``_FakeRequestBus`` idiom used across the suite."""

    def __init__(self, answer_choice_id: str) -> None:
        self._answer = answer_choice_id
        self.asks: list = []

    async def request(self, iv) -> InterventionAnswer:
        self.asks.append(iv)
        return InterventionAnswer(text=self._answer, choice_id=self._answer)


class _RecordingEvents:
    """Real EventLog-shaped collaborator (non-mock): records emitted audit-events."""

    def __init__(self) -> None:
        self.emitted: list[tuple[str, dict]] = []

    def emit(self, name: str, **payload) -> None:
        self.emitted.append((name, payload))


def _no_factory(_profile):
    raise AssertionError("session factory must not be called")


def _make_ctx(tmp_path, *, bus=None, resolver=None, events=None, state_log=None):
    """Build a production-shaped router ToolContext.

    The op-context factory mirrors what ``RouterHostAdapter.make_router_op_context``
    wires (permission_decl / intervention_bus / sandbox_policy), which is where the
    handler reads the operator's real declaration + bus from.
    """
    op_ctx = SimpleNamespace(
        permission_decl=PermissionDecl(),
        intervention_bus=bus,
        sandbox_policy=None,
        hot_reloader=None,
        events=events,
        cancel_event=None,
        agent_id=None,
    )
    return ToolContext(
        events=events if events is not None else _RecordingEvents(),
        permission_resolver=resolver,
        workspace=SimpleNamespace(root=str(tmp_path)),
        caller_kind="router",
        router_state=RouterCallerState(op_context_factory=lambda: op_ctx),
        state_log=state_log,
    )


@pytest.mark.asyncio
async def test_install_local_asks_the_operator_before_writing_recovery_core_config(tmp_path):
    """Tier 2: the write to ``.reyn/config/mcp.yaml`` is gated — the operator is ASKED.

    ``.reyn/config/`` is carved out of the default write zone, so this write has no
    standing grant and must reach the operator. Strip-falsify: point the handler's
    resolver back at ``router_state`` (a field that does not exist) and the gate goes
    silent — no ask is recorded and the config is written unasked.
    """
    (tmp_path / ".reyn").mkdir()
    bus = _RecordingBus("yes")
    resolver = PermissionResolver(config_permissions={}, project_root=tmp_path)
    ctx = _make_ctx(tmp_path, bus=bus, resolver=resolver)

    result = await MCP_INSTALL_LOCAL.handler(
        {"name": "planted", "command": "/bin/cat", "args": []}, ctx,
    )

    assert bus.asks, (
        "the operator was never asked — the recovery-core write ran unGATED "
        "(the getattr-on-an-undeclared-field bug)"
    )
    assert result["status"] == "ok"
    assert (tmp_path / ".reyn" / "config" / "mcp.yaml").is_file()


@pytest.mark.asyncio
async def test_install_local_refused_by_operator_writes_nothing(tmp_path):
    """Tier 2: a refused install must leave the operator's MCP config untouched.

    The gate is only real if its DENY is real: refusing the prompt must raise and write
    no server, else the ask is decoration.
    """
    (tmp_path / ".reyn").mkdir()
    bus = _RecordingBus("no")
    resolver = PermissionResolver(config_permissions={}, project_root=tmp_path)
    ctx = _make_ctx(tmp_path, bus=bus, resolver=resolver)

    with pytest.raises(PermissionError):
        await MCP_INSTALL_LOCAL.handler(
            {"name": "planted", "command": "/bin/cat", "args": []}, ctx,
        )

    assert not (tmp_path / ".reyn" / "config" / "mcp.yaml").exists(), (
        "a refused install must not write the config"
    )


@pytest.mark.asyncio
async def test_install_local_emits_the_installed_audit_event(tmp_path):
    """Tier 2: an install that mutates operator config leaves a P6 audit-event.

    The op path has always emitted ``mcp_server_installed``; this verb emitted nothing on
    success, so an LLM-initiated install was invisible to the operator.
    """
    (tmp_path / ".reyn").mkdir()
    events = _RecordingEvents()
    ctx = _make_ctx(tmp_path, events=events)

    await MCP_INSTALL_LOCAL.handler(
        {"name": "planted", "command": "/bin/cat", "args": []}, ctx,
    )

    names = [n for n, _ in events.emitted]
    assert "mcp_server_installed" in names, (
        f"install emitted no audit-event (saw {names}) — the mutation is unauditable"
    )


@pytest.mark.asyncio
async def test_install_local_server_survives_wal_truncation(tmp_path):
    """Tier 2: a server installed via ``mcp__install_local`` survives WAL truncation.

    Truncate-falsify (the CLAUDE.md recovery-feature gate).

    Set X → truncate past X's events → reconstruct → assert X survives. ``.reyn/config/`` is
    reconstructed from config GENERATIONS, so a writer that records none is recovery-invisible:
    once ANY generation exists for ``config/mcp.yaml`` (i.e. after any op-path install), the
    reconstruct rewrites the file from that generation and drops every server this verb added.
    """
    sl = StateLog(tmp_path / ".reyn" / "state" / "wal.jsonl")
    reg = AgentRegistry(project_root=tmp_path, session_factory=_no_factory, state_log=sl)
    mcp_path = tmp_path / ".reyn" / "config" / "mcp.yaml"
    mcp_path.parent.mkdir(parents=True, exist_ok=True)

    # An op-path install records a generation for config/mcp.yaml — this is what makes the
    # path generation-tracked, and so what makes a non-recording writer lossy.
    await reg.record_config_change("config/mcp.yaml", {"mcp": {"servers": {"A": {}}}})
    mcp_path.write_text(yaml.dump({"mcp": {"servers": {"A": {}}}}), encoding="utf-8")

    # The LLM installs B through the verb under test.
    ctx = _make_ctx(tmp_path, state_log=sl)
    await MCP_INSTALL_LOCAL.handler(
        {"name": "B", "command": "/bin/cat", "args": []}, ctx,
    )
    cut = sl.current_seq

    # The WAL advances far past the install, then GC truncates below the floor.
    for i in range(120):
        await sl.append("inbox_put", n=i)
    await sl.truncate_below(100)
    await sl.flush()

    # Reconstruct: B must still be there.
    await rewind(reg.state_log, target_n=cut)
    reg._reconcile_config_as_of_cut(cut)

    restored = yaml.safe_load(mcp_path.read_text(encoding="utf-8"))
    assert "B" in restored["mcp"]["servers"], (
        "the mcp__install_local-installed server was ERASED by config reconstruction — it "
        "recorded no truncation-surviving generation, so the reconstruct rewrote the file "
        "from the op-path generation that predates it"
    )
