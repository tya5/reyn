"""Tier 2: ChatInterventionBus stamping (issue #268 Phase 2 continuation).

PR #281 wired A2AInterventionBus to stamp ``iv.origin_channel_id``
during ``deliver`` + register its channel_id as a listener through
``send_to_agent_impl``. This PR ships the TUI-side parity:
ChatInterventionBus also stamps when constructed with an explicit
``channel_id``.

Design choice (= why optional rather than always-on):

Test fixtures construct ``ChatInterventionBus(session, run_id,
skill_name)`` directly across ~6 files. Always-on stamping would
require every fixture to ALSO register the matching channel_id as a
listener — otherwise the new origin-pin check stalls. Optional
``channel_id`` parameter keeps existing fixtures unchanged (= no
stamping → no behaviour change) and lets production sites
explicitly opt in via the new module-level
``DEFAULT_CHAT_CHANNEL_ID`` constant.

Pins:

  1. ChatInterventionBus default constructor signature (= without
     ``channel_id``) preserves pre-#268 behaviour: no stamping.
  2. With ``channel_id="<id>"``, ``deliver`` stamps
     ``iv.origin_channel_id`` if it's None.
  3. Pre-set ``iv.origin_channel_id`` is preserved (= upstream
     multi-hop delegation provenance wins).
  4. ``channel_id`` property returns the configured value (or None).
  5. ``DEFAULT_CHAT_CHANNEL_ID`` is "tui" (= matches what
     ``ChatTUIApp.on_mount`` registers as listener_id).
  6. All 3 production ChatInterventionBus construction sites pass
     ``DEFAULT_CHAT_CHANNEL_ID``.
"""
from __future__ import annotations

import asyncio
import inspect
from pathlib import Path

from reyn.chat.session import (
    DEFAULT_CHAT_CHANNEL_ID,
    ChatInterventionBus,
    ChatSession,
)
from reyn.user_intervention import (
    InterventionAnswer,
    UserIntervention,
)

# ── 1. Default constructor: no stamping (backwards-compat) ────────────


def test_chat_bus_without_channel_id_does_not_stamp(tmp_path: Path) -> None:
    """Tier 2: ``ChatInterventionBus(session, run_id, skill_name)`` —
    without ``channel_id`` arg — does NOT stamp ``iv.origin_channel_id``.

    Preserves existing test fixture compatibility (= ~6 fixtures
    construct without channel_id; they need iv to dispatch normally
    against their "test" listener registration).
    """
    session = ChatSession(agent_name="t")
    session.register_intervention_listener("test")
    bus = ChatInterventionBus(session, run_id="r1", skill_name="demo")

    async def _drive() -> str | None:
        iv = UserIntervention(kind="ask_user", prompt="?")
        task = asyncio.ensure_future(bus.deliver(iv))
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        # Resolve so deliver returns.
        await session._deliver_answer_to(iv, "ok")
        await task
        return iv.origin_channel_id

    stamped = asyncio.run(_drive())
    assert stamped is None, (
        "ChatInterventionBus without channel_id arg must NOT stamp"
    )


# ── 2. With channel_id: stamps when unset ─────────────────────────────


def test_chat_bus_with_channel_id_stamps_when_iv_origin_is_none(tmp_path: Path) -> None:
    """Tier 2: production-shape construction (= ``channel_id="tui"``)
    stamps ``iv.origin_channel_id`` to the configured value when the
    iv came in without one (= the common case for skill-emitted ivs).
    """
    session = ChatSession(agent_name="t")
    session.register_intervention_listener("tui")
    bus = ChatInterventionBus(
        session, run_id="r1", skill_name="demo", channel_id="tui",
    )

    async def _drive() -> str | None:
        iv = UserIntervention(kind="ask_user", prompt="?")
        task = asyncio.ensure_future(bus.deliver(iv))
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        await session._deliver_answer_to(iv, "ok")
        await task
        return iv.origin_channel_id

    stamped = asyncio.run(_drive())
    assert stamped == "tui"


def test_chat_bus_with_channel_id_respects_preexisting_origin(tmp_path: Path) -> None:
    """Tier 2: when an iv arrives with ``origin_channel_id`` already
    set (= e.g. upstream delegation), the bus does NOT overwrite.
    Symmetric with the A2AInterventionBus rule (PR #281).
    """
    session = ChatSession(agent_name="t")
    session.register_intervention_listener("upstream:hop")
    bus = ChatInterventionBus(
        session, run_id="r1", skill_name="demo", channel_id="tui",
    )

    async def _drive() -> str | None:
        iv = UserIntervention(
            kind="ask_user",
            prompt="?",
            origin_channel_id="upstream:hop",
        )
        task = asyncio.ensure_future(bus.deliver(iv))
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        await session._deliver_answer_to(iv, "ok")
        await task
        return iv.origin_channel_id

    stamped = asyncio.run(_drive())
    assert stamped == "upstream:hop"


# ── 3. channel_id property ───────────────────────────────────────────


def test_chat_bus_channel_id_property_returns_configured_value() -> None:
    """Tier 2: ``ChatInterventionBus.channel_id`` returns the value
    passed at construction, or None when unset.
    """
    session = ChatSession(agent_name="t")
    bus_with = ChatInterventionBus(
        session, run_id=None, skill_name=None, channel_id="tui",
    )
    assert bus_with.channel_id == "tui"

    bus_without = ChatInterventionBus(
        session, run_id=None, skill_name=None,
    )
    assert bus_without.channel_id is None


# ── 4. DEFAULT_CHAT_CHANNEL_ID is "tui" ──────────────────────────────


def test_default_chat_channel_id_constant_is_tui() -> None:
    """Tier 2: the module-level constant matches what
    ``src/reyn/chat/tui/app.py:on_mount`` registers as the listener_id.

    If either side drifts, this test catches the mismatch immediately.
    """
    assert DEFAULT_CHAT_CHANNEL_ID == "tui"


def test_tui_on_mount_registers_default_chat_channel_id_listener() -> None:
    """Tier 2: ChatTUIApp.on_mount source carries the literal "tui"
    string for ``register_intervention_listener``, matching
    ``DEFAULT_CHAT_CHANNEL_ID``.

    AST grep keeps this aligned without booting a real TUI.
    """
    from reyn.chat.tui import app as tui_app

    src = inspect.getsource(tui_app)
    # ChatTUIApp.on_mount calls
    # session.register_intervention_listener("tui").
    assert 'register_intervention_listener("tui")' in src, (
        "ChatTUIApp.on_mount must register listener_id == "
        "DEFAULT_CHAT_CHANNEL_ID. If you renamed one, rename both."
    )


# ── 5. Production construction sites pass channel_id ─────────────────


def test_all_production_chat_bus_constructions_pass_channel_id() -> None:
    """Tier 2: every in-tree ``ChatInterventionBus(...)`` construction
    in ``src/reyn/chat/session.py`` passes ``channel_id`` so the
    stamping is engaged uniformly across:

      - chat router op bus factory
      - skill-spawn bus
      - MCP-call-from-chat ad-hoc bus

    The AST-walk grep makes the contract enforceable so a future
    refactor that adds a new construction site without ``channel_id``
    fails this test before merging.
    """
    import ast

    src_path = Path(__file__).parent.parent / "src" / "reyn" / "chat" / "session.py"
    tree = ast.parse(src_path.read_text(encoding="utf-8"))

    chat_bus_calls = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        # Match ChatInterventionBus(...) — either bare name or
        # attribute access.
        func = node.func
        if isinstance(func, ast.Name) and func.id == "ChatInterventionBus":
            chat_bus_calls.append(node)
        elif isinstance(func, ast.Attribute) and func.attr == "ChatInterventionBus":
            chat_bus_calls.append(node)

    assert chat_bus_calls, (
        "Test bug — expected at least one ChatInterventionBus call site "
        "in session.py"
    )

    missing_channel_id = []
    for call in chat_bus_calls:
        kw_names = {kw.arg for kw in call.keywords if kw.arg}
        if "channel_id" not in kw_names:
            missing_channel_id.append(call.lineno)

    assert not missing_channel_id, (
        f"ChatInterventionBus construction sites at lines "
        f"{missing_channel_id} are missing the ``channel_id`` kwarg. "
        f"Production sites must pass DEFAULT_CHAT_CHANNEL_ID for "
        f"#268 Phase 2 stamping to engage."
    )


# ── 6. End-to-end: stamped iv routes through dispatch when listener present ──


def test_end_to_end_stamped_iv_with_matching_listener_dispatches(tmp_path: Path) -> None:
    """Tier 2: when a production-shape bus stamps "tui" + the TUI
    listener is registered, handle_intervention's origin-pin check
    passes + the iv dispatches normally.

    Verifies the production wiring is internally consistent (=
    DEFAULT_CHAT_CHANNEL_ID + ChatTUIApp.on_mount + handle_intervention
    Branch 3 all agree).
    """
    session = ChatSession(agent_name="t")
    session.register_intervention_listener(DEFAULT_CHAT_CHANNEL_ID)
    bus = ChatInterventionBus(
        session, run_id="r1", skill_name="demo",
        channel_id=DEFAULT_CHAT_CHANNEL_ID,
    )

    async def _drive() -> InterventionAnswer:
        iv = UserIntervention(kind="ask_user", prompt="?")
        task = asyncio.ensure_future(bus.deliver(iv))
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        # iv must NOT be stalled (= origin in listeners → dispatch path)
        assert iv.id not in session._interventions._stalled
        await session._deliver_answer_to(iv, "ok")
        return await task

    answer = asyncio.run(_drive())
    assert answer.text == "ok"


def test_end_to_end_stamped_iv_without_listener_stalls(tmp_path: Path) -> None:
    """Tier 2: when a production-shape bus stamps "tui" + the "tui"
    listener is NOT registered, the iv stalls.

    This is the new behaviour the stamping enables: a stamped iv in
    a session without the matching listener becomes observable in
    ``list_stalled_interventions`` instead of hanging forever in the
    handler.

    The test only registers a different listener (= simulating "no
    real TUI but some other listener active for unrelated reasons")
    so the Phase 1 subscriber-presence guard doesn't kick in for the
    short-circuit path.

    The actual stall check fires in ``_dispatch_intervention`` (=
    moved there from handle_intervention so bus.deliver also benefits
    — issue #268 Phase 2 continuation).
    """
    session = ChatSession(agent_name="t")
    # Register a NON-tui listener — passes Phase 1 guard but doesn't
    # match the bus's channel_id stamp.
    session.register_intervention_listener("other")
    bus = ChatInterventionBus(
        session, run_id="r1", skill_name="demo",
        channel_id=DEFAULT_CHAT_CHANNEL_ID,
    )

    async def _drive() -> None:
        iv = UserIntervention(kind="ask_user", prompt="?")
        task = asyncio.ensure_future(bus.deliver(iv))
        # Let deliver progress to the await point.
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        # iv must be in stalled queue.
        assert iv.id in session._interventions._stalled
        # Clean up.
        await session.discard_pending_intervention(iv.id)
        await task

    asyncio.run(_drive())


# ── 7. Override-aware stamping skip (A2A-spawned skill case) ───────


def test_chat_bus_skips_stamping_when_chain_override_active(tmp_path: Path) -> None:
    """Tier 2: when a chain override is registered for the iv's run_id
    chain (= A2A async task path), ChatInterventionBus does NOT stamp.
    The downstream override bus (A2AInterventionBus) gets a clean
    ``origin_channel_id=None`` slot to stamp ``a2a:<run_id>`` instead.

    Critical correctness contract: WITHOUT this skip, A2A-spawned skill
    ivs would carry ``origin_channel_id="tui"`` (= bus default), then
    in _dispatch_intervention the override path runs FIRST (= delivers
    to A2A peer correctly), but the iv body that the peer sees has
    "tui" as the origin — a wrong provenance claim. The peer's ack /
    observe / claim machinery (= future Phase) would route based on
    that wrong origin.
    """
    session = ChatSession(agent_name="t")

    # Simulate A2A-style override registration for chain "chain-A2A".
    class _CapturingOverride:
        def __init__(self) -> None:
            self.received: list[UserIntervention] = []

        async def request(self, iv: UserIntervention) -> InterventionAnswer:
            self.received.append(iv)
            return InterventionAnswer(text="from-override")

    override = _CapturingOverride()
    session.register_intervention_override("chain-A2A", override)
    # Wire run_id → chain mapping so _dispatch_intervention can resolve.
    session.running_skills_chain["run-A2A"] = "chain-A2A"

    bus = ChatInterventionBus(
        session, run_id="run-A2A", skill_name="demo",
        channel_id=DEFAULT_CHAT_CHANNEL_ID,
    )

    async def _drive() -> str | None:
        iv = UserIntervention(kind="ask_user", prompt="?")
        answer = await bus.deliver(iv)
        assert answer.text == "from-override"
        return iv.origin_channel_id

    stamped = asyncio.run(_drive())
    assert stamped is None, (
        f"ChatInterventionBus must NOT stamp origin_channel_id when "
        f"chain override is active (got {stamped!r}); leave the slot "
        f"clean so A2AInterventionBus.deliver downstream can stamp "
        f"the correct a2a:<run_id> value."
    )
    # And the override actually saw the iv.
    assert len(override.received) == 1


def test_chat_bus_stamps_when_no_chain_override_active(tmp_path: Path) -> None:
    """Tier 2: when no chain override exists for the iv's run_id (=
    TUI-only path), stamping engages normally. Differentiates against
    the override-active branch.
    """
    session = ChatSession(agent_name="t")
    session.register_intervention_listener("tui")
    bus = ChatInterventionBus(
        session, run_id="run-TUI", skill_name="demo",
        channel_id=DEFAULT_CHAT_CHANNEL_ID,
    )

    async def _drive() -> str | None:
        iv = UserIntervention(kind="ask_user", prompt="?")
        task = asyncio.ensure_future(bus.deliver(iv))
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        await session._deliver_answer_to(iv, "ok")
        await task
        return iv.origin_channel_id

    stamped = asyncio.run(_drive())
    assert stamped == "tui"


# ── 8. Phase 1 mechanism now fires for bus path too ──────────────────


def test_dispatch_intervention_stall_check_fires_from_bus_path(tmp_path: Path) -> None:
    """Tier 2: the origin-pin stall check moved from
    ``handle_intervention`` Branch 3 into ``_dispatch_intervention`` so
    bus.deliver path benefits too.

    Verifies the move directly: invoking ``_dispatch_intervention``
    with a stamped iv whose listener is absent puts the iv in the
    stalled queue + emits a ``user_channel_stalled`` route event.
    """
    session = ChatSession(agent_name="t")
    session.register_intervention_listener("other")

    routed_events: list[dict] = []

    def _capture(ev) -> None:
        if ev.type == "intervention_routed":
            routed_events.append(dict(ev.data))

    session._chat_events.add_subscriber(_capture)

    async def _drive() -> None:
        iv = UserIntervention(
            kind="ask_user", prompt="?",
            origin_channel_id="tui",  # stamped origin not in listeners
        )
        task = asyncio.ensure_future(session._dispatch_intervention(iv))
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert iv.id in session._interventions._stalled
        await session.discard_pending_intervention(iv.id)
        await task

    asyncio.run(_drive())

    stalled = [
        e for e in routed_events
        if e.get("route") == "user_channel_stalled"
    ]
    assert len(stalled) == 1
    assert stalled[0]["origin_channel_id"] == "tui"
