"""Tier 2: #5091 — the broker-participation use case #5084 ③-b built a
dedicated mechanism for (`broker_hooks.py`, `AgentProfile.broker_identity`,
a derived hook-registry layer) is fully served by the ALREADY-EXISTING
per-session hooks layer (#2285, `Session._read_per_session_hooks`) — no
mechanism was lost by removing the dedicated one.

Owner ruling (relayed via architect): "broker" is an external MCP server,
not a reyn-runtime concept — the derivation baked a specific integration's
name (`"server": "broker"`) into reyn's own source, and #5012-A's own
discriminator ("would another integration need the SAME code?" — yes,
`slack_identity` + `derive_slack_hooks` — disqualified) applies. Architect's
own measured finding: `_read_per_session_hooks`'s own docstring already
says a hook defined there "is visible ONLY to this session" — exactly the
scoping a per-agent broker-inbox subscription needs, so a HAND-WRITTEN,
literal (non-templated) hook in that file already does the job; no
identity field, no derivation function, no dedicated layer.

Real `Session` construction (`make_session`, the same helper #2073's own
per-agent-hooks tests use) — no mocks.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

from reyn.core.events.state_log import StateLog
from reyn.runtime.session import Session
from reyn.runtime.session_params import ReactivityConfig
from reyn.security.sandbox.noop_backend import NoopBackend
from tests._support.agent_session import make_session


def _make_session(
    tmp_path: Path,
    agent_name: str,
    *,
    workspace_base_dir: "Path | None" = None,
    hooks_config: "list | None" = None,
) -> Session:
    """``hooks_config`` (#5356/#5360, added for the ``session_start``
    exec-hook test below): threads through ``ReactivityConfig.hooks_config``
    to ``Session._startup_hooks_raw`` (origin ``"startup"``) — the same
    layer a real ``reyn.yaml`` occupies, and NOT agent-writable, unlike the
    per-agent/per-session layers ``_write_broker_inbox_hook`` below writes
    to."""
    return make_session(
        agent_name=agent_name,
        state_log=StateLog(tmp_path / f"{agent_name}.wal"),
        snapshot_path=tmp_path / agent_name / "snap.json",
        reactivity=ReactivityConfig(hooks_config=hooks_config),
        workspace_base_dir=workspace_base_dir,
        sandbox_backend=NoopBackend(),
    )


async def _wait_for(predicate) -> None:
    """Poll until the dispatched hook's real side effect becomes
    observable. Unbounded per this repo's own testing policy (no time
    budget in a test body) — CI's own --timeout is the kill switch."""
    while not predicate():
        await asyncio.sleep(0.02)


def _write_broker_inbox_hook(
    session: Session, *, own_identity: str, with_register_script: bool = False,
) -> None:
    """The hand-written form architect's own #5091 demonstration used —
    a LITERAL uri, no token/templating, because the per-session hooks
    layer is already scoped to exactly one agent's own session.

    ``with_register_script`` (lead-coder's own #5095 review finding,
    issuecomment-5379677121): the REAL shape has 2 hooks, not 1 — the
    ``mcp_resource_updated`` push above, PLUS a ``session_start`` ``exec``
    hook (``network: true``, relative argv) that registers the identity
    with the broker at boot. The 2nd hook is the one whose "can this be
    hand-written?" is actually in question (the 1st is a literal string
    with no path resolution involved at all); this flag lets the 2 tests
    below share the writer without the simpler test paying for a script
    file it doesn't use."""
    snapshot_dir = Path(session._snapshot_path).parent
    snapshot_dir.mkdir(parents=True, exist_ok=True)
    hooks_yaml = (
        "hooks:\n"
        "  - on: mcp_resource_updated\n"
        "    matcher:\n"
        "      server: broker\n"
        f"      uri: \"broker://inbox/{own_identity}\"\n"
        "    template_push:\n"
        "      message: \"new broker message\"\n"
        "      wake: true\n"
    )
    if with_register_script:
        hooks_yaml += (
            "  - on: session_start\n"
            "    network: true\n"
            "    exec:\n"
            f"      - {sys.executable}\n"
            "      - register_with_broker.py\n"
        )
    (snapshot_dir / "hooks.yaml").write_text(hooks_yaml, encoding="utf-8")


def test_two_agents_each_subscribe_their_own_broker_inbox_with_no_derivation(
    tmp_path: Path,
):
    """Tier 2: #5091's own acceptance witness — 2 agents, each with a
    hand-written per-session `hooks.yaml` naming its OWN broker inbox URI,
    resolve to 2 DIFFERENT `mcp_resource_updated` matchers — neither sees
    the other's, and neither required any per-agent identity field or
    derivation function. Real ``Session._build_hook_registry`` (the
    layered COMBINE), not a stand-in."""
    session_a = _make_session(tmp_path, "coder-smith")
    session_b = _make_session(tmp_path, "coder-brown")
    _write_broker_inbox_hook(session_a, own_identity="coder-smith")
    _write_broker_inbox_hook(session_b, own_identity="coder-brown")

    registry_a = session_a._build_hook_registry()
    registry_b = session_b._build_hook_registry()

    (hook_a,) = registry_a.hooks_for("mcp_resource_updated")
    (hook_b,) = registry_b.hooks_for("mcp_resource_updated")
    assert hook_a.matcher == {"server": "broker", "uri": "broker://inbox/coder-smith"}
    assert hook_b.matcher == {"server": "broker", "uri": "broker://inbox/coder-brown"}


async def _drain_texts(session: Session) -> set:
    """#5091 witness (lead-coder's own point): the property under test here
    is DELIVERY, not registration — the test above already proves each
    session's hook REGISTRY resolves to its own matcher config statically;
    this proves an actual dispatched event reaches the RIGHT session's own
    inbox and not the other's. Mirrors ``test_2073_per_agent_hooks.py``'s
    own helper of the same name/shape (the public inbox is the observation
    point both modules use)."""
    texts = set()
    while not session.inbox.empty():
        _kind, payload = session.inbox.get_nowait()
        texts.add(payload.get("text"))
    return texts


@pytest.mark.asyncio
async def test_a_resource_update_reaches_only_the_matching_agents_own_session(
    tmp_path: Path,
):
    """Tier 2: #5091's own acceptance point (lead-coder, not architect's
    static-registration witness above) — "hooks が読まれた" is not the
    claim; "別々の inbox に別々に届く" is. Drives the REAL
    ``Session._hook_dispatcher.dispatch("mcp_resource_updated", ...)`` —
    the production entry point ``McpIngressAdapter``/``MCPConnectionService``
    ultimately call (see ``dispatcher.py``'s own ``dispatch`` docstring) —
    against BOTH sessions for coder-smith's own uri, then the symmetric
    case for coder-brown's: each fires ONLY its own agent's hook, never
    the other's. No mocks — 2 real ``Session``s, the real dispatcher, the
    real matcher (``reyn.hooks.matcher.matches`` via ``event_pattern``)."""
    session_a = _make_session(tmp_path, "coder-smith")
    session_b = _make_session(tmp_path, "coder-brown")
    _write_broker_inbox_hook(session_a, own_identity="coder-smith")
    _write_broker_inbox_hook(session_b, own_identity="coder-brown")
    # #2073 S2b's own reapply seam (``replace_registry``, docstring above):
    # each dispatcher's registry was built at Session construction, BEFORE
    # the hooks.yaml above existed — re-read it now, the same call
    # production makes at the turn boundary, so the dispatcher below
    # actually sees what was just written.
    session_a._hook_dispatcher.replace_registry(session_a._build_hook_registry())
    session_b._hook_dispatcher.replace_registry(session_b._build_hook_registry())

    def _mcp_event(uri: str, agent_name: str) -> dict:
        return {
            "point": "mcp_resource_updated", "server": "broker",
            "uri": uri, "agent_name": agent_name, "resync": False,
        }

    smith_uri = "broker://inbox/coder-smith"
    await session_a._hook_dispatcher.dispatch(
        "mcp_resource_updated", _mcp_event(smith_uri, "coder-smith"),
    )
    await session_b._hook_dispatcher.dispatch(
        "mcp_resource_updated", _mcp_event(smith_uri, "coder-smith"),
    )
    assert await _drain_texts(session_a) == {"new broker message"}, (
        "coder-smith's own inbox uri must fire coder-smith's hook"
    )
    assert await _drain_texts(session_b) == set(), (
        "coder-smith's uri must NOT fire coder-brown's hook — cross-delivery"
    )

    brown_uri = "broker://inbox/coder-brown"
    await session_a._hook_dispatcher.dispatch(
        "mcp_resource_updated", _mcp_event(brown_uri, "coder-brown"),
    )
    await session_b._hook_dispatcher.dispatch(
        "mcp_resource_updated", _mcp_event(brown_uri, "coder-brown"),
    )
    assert await _drain_texts(session_a) == set(), (
        "coder-brown's uri must NOT fire coder-smith's hook — cross-delivery"
    )
    assert await _drain_texts(session_b) == {"new broker message"}, (
        "coder-brown's own inbox uri must fire coder-brown's hook"
    )


def test_no_hooks_yaml_means_no_broker_subscription(tmp_path: Path):
    """Tier 2: regression guard — an agent with no per-session `hooks.yaml`
    at all (every agent that isn't opting into broker participation,
    including the default agent) subscribes nothing. Absence must not
    silently opt an agent in."""
    session = _make_session(tmp_path, "plain-agent")
    registry = session._build_hook_registry()
    assert registry.hooks_for("mcp_resource_updated") == []


def test_strip_falsifier_removing_the_per_session_read_breaks_the_witness(
    tmp_path: Path, monkeypatch,
):
    """Tier 2: strip-falsifier — with `_read_per_session_hooks` forced to
    return `[]` (simulating its removal from `_build_hook_registry`'s
    COMBINE), the SAME per-session `hooks.yaml` this module's own positive
    witness relies on no longer resolves — confirming the positive witness
    genuinely depends on that layer, not on some other coincidental path."""
    session = _make_session(tmp_path, "coder-smith")
    _write_broker_inbox_hook(session, own_identity="coder-smith")
    monkeypatch.setattr(session, "_read_per_session_hooks", lambda: [])

    registry = session._build_hook_registry()

    assert registry.hooks_for("mcp_resource_updated") == []


# ── the 2nd hook: session_start exec, relative argv, agent's own base_dir ──


@pytest.mark.asyncio
async def test_session_start_exec_hook_resolves_the_register_script_in_its_own_base_dir(
    tmp_path: Path, monkeypatch,
):
    """Tier 2: lead-coder's own #5095 review finding — the FIRST hook
    (a literal ``uri`` string) is trivially hand-writable, but the SECOND
    (a ``session_start`` ``exec`` hook running ``register_with_broker.py``
    via a RELATIVE argv) is the one whose "can this really be hand-written,
    with no derivation?" was actually in question, per the deleted
    ``broker_hooks.py``'s own docstring ("Relative argv (#5084 ④): resolves
    against THIS agent's own base_dir via HookDispatcher's hook_cwd").

    Drives the REAL ``Session._hook_dispatcher.dispatch("session_start",
    ...)`` — the production code path, not a stand-alone `HookDispatcher`
    — with a real ``NoopBackend`` executing a real (if trivial)
    ``register_with_broker.py`` placed in the agent's own `base_dir`. The
    script writes a marker file using only its OWN cwd (`Path("registered
    ").write_text(...)`, no absolute path), so the marker landing in the
    RIGHT agent's directory is only possible if `hook_cwd` genuinely
    resolved to THIS agent's own `base_dir` — the exact #5084 ④ mechanism
    #5091 kept unmodified."""
    monkeypatch.setenv("REYN_ACCEPT_HOOKS", "1")
    base_dir = tmp_path / "coder-smith-base"
    base_dir.mkdir(parents=True)
    (base_dir / "register_with_broker.py").write_text(
        "from pathlib import Path\n"
        "Path('registered.marker').write_text('coder-smith')\n",
        encoding="utf-8",
    )

    # #5356/#5360: `network: true` at an agent-writable origin (per-agent,
    # per-session — an agent can already write either via the ordinary
    # file-write op / `hooks_add`) is now an eager-rejected confused-deputy
    # self-grant, dropping the whole layer. This test used to write this
    # exact declaration into the per-session layer; it is REWRITTEN here
    # to the startup layer instead — matching the REAL deployed shape
    # (lead-coder's own measurement, 2026-08-24: every live `network: true`
    # hook declaration already lives in `reyn.yaml`'s startup layer, none
    # in per-agent/per-session, which are both empty), not a design
    # regression. `_make_session`'s `hooks_config` threads to
    # `Session._startup_hooks_raw` (origin "startup", not agent-writable,
    # so #5356's rejection does not apply here). The property this test
    # verifies — a `session_start` exec hook's relative argv resolves
    # against THIS agent's own `base_dir` via `HookDispatcher`'s
    # `hook_cwd` — depends on `workspace_base_dir`/dispatch, not on which
    # config layer supplied the hook, so it is unaffected by this move.
    session = _make_session(
        tmp_path,
        "coder-smith",
        workspace_base_dir=base_dir,
        hooks_config=[
            {
                "on": "session_start",
                "network": True,
                "exec": [sys.executable, "register_with_broker.py"],
            },
        ],
    )

    await session._hook_dispatcher.dispatch("session_start", {})

    await _wait_for(lambda: (base_dir / "registered.marker").exists())
    assert (base_dir / "registered.marker").read_text(encoding="utf-8") == "coder-smith"
