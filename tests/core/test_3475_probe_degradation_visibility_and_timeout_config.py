"""Tier 2/1: #3475 (remaining half) — probe-degradation visibility + `per_server_timeout`
config-isation.

The turn-kind priming gap (#3475's first half) landed in PR #3499. This file covers the
owner-decided remainder (see the issue's final two comments): `ensure_mcp_tools_cached`'s
`_probe_one` used to cache a timed-out/broken MCP server as an empty tool list for the
session's entire life and say NOTHING about it — an operator could only notice via a
fixture byte-diff, as #3475's own investigation did. The owner's decision was BOTH of:

  C — surface the degradation as an `mcp_tool_probe_degraded` audit-event naming the
      server and the reason (`timeout` / `exception`).
  config-isation — the two independently-hardcoded `per_server_timeout: float = 5.0`
      defaults (`router_host_adapter.py`'s `ensure_mcp_tools_cached` and
      `interfaces/cli/commands/mcp.py`'s `_probe_server_tools`) now derive from ONE
      source: `TimeoutConfig.mcp_probe_seconds` (`safety.timeout.mcp_probe_seconds` in
      reyn.yaml), default unchanged at 5.0.

Because C alone tells the operator "you're losing" but not how to fix it, and the config
knob alone lets them fix a problem they cannot notice, the owner required both in one PR
(see the issue's last two comments — "C対応必須だし、5.0 ハードコードは修正必要").

No unittest.mock/AsyncMock/MagicMock/patch anywhere in this file. The Session-level tests
use the SAME real-callable-override technique `tests/core/test_mcp_lazy_tools_cache.py` and
`tests/runtime/test_3475_mcp_probe_priming_all_turn_kinds.py` already use (instance-attribute
assignment of a plain async function onto `router_host.mcp_list_tools`) — not a mock.

The single most important property asserted here is NOT "the mechanism ran" — it is what
the operator/LLM actually receives: the literal audit-event payload
(`event.type`, `event.data["server"]`, `event.data["reason"]`) an operator reading
`.reyn/events` would see, and the
literal wall-clock behaviour change (timeout fires or doesn't) a non-default config value
produces. #3512's strip-D arm went GREEN because every witness there was "a function was
called", never "the string a consumer reads" — this file assigns the audit-event's own
field values, not a call count, and it drives a NON-default config value end to end
(a probe that would only time out under the CONFIGURED value, not under the still-live
5.0 default) so a dead wiring shows up as a wrong RESULT, not merely an uncalled path.

strip-falsify (recorded here, executed manually before landing):
  - Visibility: revert the two `self._events.emit("mcp_tool_probe_degraded", ...)` calls
    in `_probe_one` back to a bare `return server_name, []` → RED (no event of that type).
  - Config wiring: revert `Session._handle_user_message` / `Session._run_router_loop` to
    call `ensure_mcp_tools_cached()` with no `per_server_timeout=` kwarg → RED (the 0
    configured value never reaches the probe, the unwired 5.0s default lets the
    near-instant yielding probe complete normally, the server IS cached with tools, and
    `test_session_threads_configured_probe_timeout...` fails on the "absent from cache"
    assertion). #4264 ⑤ replaced the original 0.05s-configured/0.2s-sleeping-probe/5.0s-
    default three-value race with `per_server_timeout=0` + a probe that only YIELDS
    (`await asyncio.sleep(0)`) — reyn's own responsibility ends at "which value reaches
    `asyncio.timeout()`", so the test no longer needs a real elapsed-time race to prove it.
"""
from __future__ import annotations

import ast
import asyncio
import os
from pathlib import Path

import pytest

from reyn.config import SafetyConfig, TimeoutConfig
from reyn.core.events.event_schema import AUDIT_EVENT_KINDS
from reyn.core.events.state_log import StateLog
from tests._support.agent_session import make_session
from tests._support.events import collect_events, settle
from tests._support.paths import REPO_ROOT

_REPO = REPO_ROOT
_ROUTER_HOST_ADAPTER = _REPO / "src" / "reyn" / "runtime" / "services" / "router_host_adapter.py"
_MCP_CLI = _REPO / "src" / "reyn" / "interfaces" / "cli" / "commands" / "mcp.py"
_CHAT_CONFIG = _REPO / "src" / "reyn" / "config" / "chat.py"


# ── 1. Config contract — `safety.timeout.mcp_probe_seconds` parses (Tier 1) ────────────


def test_mcp_probe_seconds_default_and_nondefault_parse():
    """Tier 1: `TimeoutConfig.mcp_probe_seconds` defaults to 5.0 (unchanged — this is a
    knob, not a default-raise) and `_build_safety_config` round-trips a non-default
    operator value from the `safety.timeout:` yaml section."""
    from reyn.config.chat import _build_safety_config

    assert TimeoutConfig().mcp_probe_seconds == 5.0
    assert SafetyConfig().timeout.mcp_probe_seconds == 5.0

    built_default = _build_safety_config({})
    assert built_default.timeout.mcp_probe_seconds == 5.0

    built = _build_safety_config({"timeout": {"mcp_probe_seconds": 12.5}})
    assert built.timeout.mcp_probe_seconds == 12.5, (
        "a non-default safety.timeout.mcp_probe_seconds must round-trip through "
        "_build_safety_config — testing with 5.0 itself would stay green even if "
        "the key were never read"
    )


# ── 2. `mcp_tool_probe_degraded` is a declared audit-event kind (Tier 1) ───────────────


def test_mcp_tool_probe_degraded_is_a_declared_kind():
    """Tier 1: the new audit-event kind is in the closed vocabulary (#3410) —
    tests/core/test_audit_event_kind_vocabulary_3410.py additionally checks the doc mirror
    and that every declared kind has a real emit site."""
    assert "mcp_tool_probe_degraded" in AUDIT_EVENT_KINDS


# ── 3. Session-level: non-default config value actually bounds the probe,          ────
#        AND the degradation is visible on the real EventLog (Tier 2)              ────


def _make_probe_session(tmp_path: Path, *, mcp_probe_seconds: float, probe_cb):
    server = "flaky_server"
    session = make_session(
        agent_name="fp3475-probe-agent",
        mcp_servers={server: {"description": "flaky"}},
        state_log=StateLog(tmp_path / "state.wal"),
        snapshot_path=tmp_path / "snapshot.json",
        safety=SafetyConfig(timeout=TimeoutConfig(mcp_probe_seconds=mcp_probe_seconds)),
    )
    # Real-callable override (same technique as test_mcp_lazy_tools_cache.py /
    # test_3475_mcp_probe_priming_all_turn_kinds.py) — not a mock.
    session.router_host.mcp_list_tools = probe_cb
    return session, server


async def _drive_first_turn(session) -> None:
    """Route one turn through the real turn-kind seam (`_run_router_loop`).

    #5103: the caller declares `@pytest.mark.llm_stub` — the REAL
    `RouterLoopDriver.run_turn` / `RouterLoop` chain runs; only the LLM
    boundary itself (`litellm.acompletion`) is stubbed (architect design
    "C2", #5363's own precedent). Before this, `session._loop_driver.
    run_turn` was replaced wholesale with a private `_noop`, so this
    file's subject (does the priming chain's ARGUMENTS/audit-events
    actually reach a real turn) was never witnessed on a real code path."""
    await session.submit_agent_request(
        from_agent="peer", request="hello", depth=1, chain_id="chain-3475",
    )
    await session.run_one_iteration()


@pytest.mark.asyncio
@pytest.mark.llm_stub
async def test_session_threads_configured_probe_timeout_into_the_real_probe(tmp_path: Path):
    """Tier 2: (LOAD-BEARING) `safety.timeout.mcp_probe_seconds` set to a NON-default
    value (0) must reach `ensure_mcp_tools_cached`'s `per_server_timeout` — reyn's own
    responsibility here is only WHICH VALUE reaches `asyncio.timeout(per_server_timeout)`
    (#4264 ⑤, owner ruling: reyn calls an existing stdlib timeout mechanism, it does not
    implement one, so the test needs a VALUE distinction, not a real elapsed-time race).
    A probe that yields once (`await asyncio.sleep(0)`, a scheduler yield — not a wait)
    is used against `per_server_timeout=0`: `asyncio.timeout(0)` expires at the FIRST
    checkpoint, so a wired path ALWAYS times out (server left UNANSWERED — #3520: absent
    from the cache, not cached empty) with zero real elapsed time, while a dead path
    (still passing the unchanged 5.0s default) lets the near-instant probe complete
    normally and record its tools — the test fails on a wrong RESULT, not merely on an
    uncalled function, if the wiring from Session._safety.timeout.mcp_probe_seconds
    regresses, and it does so without racing real wall-clock time against a chosen
    constant (the old 0.05s-vs-0.2s-vs-5.0s three-value race this replaces)."""
    old_cwd = os.getcwd()
    os.chdir(tmp_path)
    try:
        async def _yielding_probe(server_name: str) -> list[dict]:
            await asyncio.sleep(0)  # a scheduler yield, not a wait — see docstring
            return [{"name": "should_not_appear", "description": ""}]

        session, server = _make_probe_session(
            tmp_path, mcp_probe_seconds=0, probe_cb=_yielding_probe,
        )
        await _drive_first_turn(session)

        snapshot = session.router_host.mcp_tools_cache_snapshot
        assert snapshot is not None, "priming must have run for the first turn"
        assert server not in snapshot, (
            "expected the configured 0-second per_server_timeout to time out the "
            "yielding probe at its first checkpoint, leaving the server unanswered "
            f"and therefore absent from the cache; got {snapshot!r}. A recorded "
            "entry means the 0 value never reached ensure_mcp_tools_cached and the "
            "unwired 5.0s default absorbed the (near-instant) probe instead"
        )
    finally:
        os.chdir(old_cwd)


@pytest.mark.asyncio
@pytest.mark.llm_stub
async def test_probe_timeout_emits_visible_degradation_event(tmp_path: Path):
    """Tier 2: (LOAD-BEARING) a probe that times out under the configured budget emits
    `mcp_tool_probe_degraded` on the session's real EventLog, naming the server and
    `reason="timeout"` — the literal payload an operator reading `.reyn/events` sees,
    not merely evidence that some emit call happened. See the sibling test above for
    why `per_server_timeout=0` + a yielding (not sleeping) probe replaces a real-time
    race (#4264 ⑤)."""
    old_cwd = os.getcwd()
    os.chdir(tmp_path)
    try:
        async def _yielding_probe(server_name: str) -> list[dict]:
            await asyncio.sleep(0)  # a scheduler yield, not a wait — see sibling test
            return [{"name": "x", "description": ""}]

        session, server = _make_probe_session(
            tmp_path, mcp_probe_seconds=0, probe_cb=_yielding_probe,
        )
        collected = collect_events(session.router_host.events)
        await _drive_first_turn(session)
        await settle(session.router_host.events)

        degradations = [
            e for e in collected
            if e.type == "mcp_tool_probe_degraded"
        ]
        assert len(degradations) >= 1, (
            "no mcp_tool_probe_degraded event was emitted — the probe timeout is "
            "silent again"
        )
        event = degradations[0]
        assert event.data["server"] == server
        assert event.data["reason"] == "timeout"
        assert event.data["per_server_timeout"] == 0, (
            "the event should name the ACTUAL timeout budget in force, not a "
            "hardcoded literal"
        )
    finally:
        os.chdir(old_cwd)


@pytest.mark.asyncio
@pytest.mark.llm_stub
async def test_probe_exception_emits_visible_degradation_event(tmp_path: Path):
    """Tier 2: a probe that raises (broken server, not merely slow) also emits
    `mcp_tool_probe_degraded`, with `reason="exception"` and a `detail` naming the
    error — independent of the configured timeout value (default budget here)."""
    old_cwd = os.getcwd()
    os.chdir(tmp_path)
    try:
        async def _broken_probe(server_name: str) -> list[dict]:
            raise RuntimeError("connection refused")

        session, server = _make_probe_session(
            tmp_path, mcp_probe_seconds=TimeoutConfig().mcp_probe_seconds,
            probe_cb=_broken_probe,
        )
        collected = collect_events(session.router_host.events)
        await _drive_first_turn(session)
        await settle(session.router_host.events)

        degradations = [
            e for e in collected
            if e.type == "mcp_tool_probe_degraded"
        ]
        assert len(degradations) >= 1
        event = degradations[0]
        assert event.data["server"] == server
        assert event.data["reason"] == "exception"
        assert "connection refused" in event.data.get("detail", ""), (
            f"expected the underlying error in `detail`, got {event.data!r}"
        )
    finally:
        os.chdir(old_cwd)


# ── 4. AST completeness — exactly ONE literal 5.0 default, the other two derive ────
#        from it (Tier 2) ──────────────────────────────────────────────────────────


def _kwonly_default_node(tree: ast.Module, func_name: str, param_name: str) -> ast.expr | None:
    """Return the AST default expression for `param_name` (a kwonly arg) of the
    (sole) function/async-function named `func_name` in `tree`."""
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == func_name:
            names = [a.arg for a in node.args.kwonlyargs]
            defaults = node.args.kw_defaults
            for name, default in zip(names, defaults):
                if name == param_name:
                    return default
    return None


def _is_float_literal_5(node: ast.expr | None, value: float = 5.0) -> bool:
    return (
        isinstance(node, ast.Constant)
        and isinstance(node.value, (int, float))
        and float(node.value) == value
    )


def _dataclass_field_default(tree: ast.Module, class_name: str, field_name: str) -> ast.expr | None:
    """Return the AST expression this field's default actually evaluates to.

    #4206 wrapped every `ReynConfig` leaf's declaration in
    `field(default=..., metadata={...})` (axis metadata lives on the field, not
    inferred) — so `stmt.value` is now an `ast.Call` to `field(...)`, not the
    bare literal. Unwrap it to the `default=` keyword's own value, which is
    what this test actually cares about (is the CANONICAL default a literal
    5.0), not the declaration's outer shape.
    """
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == class_name:
            for stmt in node.body:
                if (
                    isinstance(stmt, ast.AnnAssign)
                    and isinstance(stmt.target, ast.Name)
                    and stmt.target.id == field_name
                    and stmt.value is not None
                ):
                    value = stmt.value
                    if (
                        isinstance(value, ast.Call)
                        and isinstance(value.func, ast.Name)
                        and value.func.id == "field"
                    ):
                        for kw in value.keywords:
                            if kw.arg == "default":
                                return kw.value
                        return None  # default_factory or no default at all
                    return value
    return None


def test_per_server_timeout_derives_from_a_single_5_0_source():
    """Tier 2: (LOAD-BEARING, AST — not regex, since a regex for `5.0` catches
    docstring/comment mentions of the number as false positives) exactly ONE literal
    `5.0` default exists across the three sites this feature touches:
    `TimeoutConfig.mcp_probe_seconds` (the config definition — #3461's `FileScopes`
    precedent: the default lives on the schema, not hidden in a resolver's
    `__init__`). `RouterHostAdapter.ensure_mcp_tools_cached`'s `per_server_timeout`
    and `interfaces/cli/commands/mcp.py`'s `_probe_server_tools`'s `per_server_timeout`
    must NOT independently repeat the literal — both derive their default from the
    `TimeoutConfig` field so raising the one number is the only edit needed."""
    chat_tree = ast.parse(_CHAT_CONFIG.read_text(encoding="utf-8"))
    adapter_tree = ast.parse(_ROUTER_HOST_ADAPTER.read_text(encoding="utf-8"))
    cli_tree = ast.parse(_MCP_CLI.read_text(encoding="utf-8"))

    config_default = _dataclass_field_default(chat_tree, "TimeoutConfig", "mcp_probe_seconds")
    assert config_default is not None, "TimeoutConfig.mcp_probe_seconds field not found"
    assert _is_float_literal_5(config_default), (
        "the canonical default must live on TimeoutConfig.mcp_probe_seconds as the "
        "literal 5.0 — found something else"
    )

    adapter_default = _kwonly_default_node(
        adapter_tree, "ensure_mcp_tools_cached", "per_server_timeout",
    )
    assert adapter_default is not None, "ensure_mcp_tools_cached.per_server_timeout not found"
    assert not _is_float_literal_5(adapter_default), (
        "ensure_mcp_tools_cached's per_server_timeout default must NOT independently "
        "hardcode 5.0 — it must derive from TimeoutConfig.mcp_probe_seconds instead"
    )

    cli_default = _kwonly_default_node(cli_tree, "_probe_server_tools", "per_server_timeout")
    assert cli_default is not None, "_probe_server_tools.per_server_timeout not found"
    assert not _is_float_literal_5(cli_default), (
        "_probe_server_tools's per_server_timeout default must NOT independently "
        "hardcode 5.0 — it must derive from TimeoutConfig.mcp_probe_seconds instead"
    )
