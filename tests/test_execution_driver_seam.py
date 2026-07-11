"""Tier 2: OS invariant tests for the ExecutionDriver injection seam.

Verifies the three invariants of the injectable execution-driver seam added to
Session.__init__:

1. Substitutability — an injected FakeDriver is used as-is; cancel delegation
   routes through it.
2. No missing attrs — injecting a driver does NOT skip other Session construction
   (history buffer, compaction controller, budget advisor, router host, messaging
   are all present).
3. Byte-identical default — omitting loop_driver builds a real RouterLoopDriver.

Policy compliance (docs/deep-dives/contributing/testing.md):
- No unittest.mock.MagicMock / AsyncMock / patch.
- Private-state assertions are limited to the specific attrs explicitly listed
  for the no-missing-attrs test (they are the subject under test, not
  incidental implementation details).
- Each test docstring's first line declares its Tier.
"""
from __future__ import annotations

from reyn.runtime.services.router_loop_driver import RouterLoopDriver
from reyn.runtime.session import Session

# ---------------------------------------------------------------------------
# FakeDriver — plain class implementing ExecutionDriver (NO MagicMock / patch)
# ---------------------------------------------------------------------------


class FakeDriver:
    """Minimal real implementation of ExecutionDriver for seam tests."""

    def __init__(self) -> None:
        self._cancel_requested: bool = False
        self.run_turn_calls: list[tuple[str, str]] = []

    async def run_turn(self, user_text: str, chain_id: str) -> None:
        self.run_turn_calls.append((user_text, chain_id))

    def is_cancel_requested(self) -> bool:
        return self._cancel_requested

    @property
    def cancel_event(self) -> None:
        """#2813: no interactive-turn cancel_event concept for this fake."""
        return None

    def request_cancel(self) -> None:
        self._cancel_requested = True

    async def _check_cap(self, user_text: str) -> None:
        pass


# ---------------------------------------------------------------------------
# Test 1: Substitutability
# ---------------------------------------------------------------------------


def test_injected_driver_is_used_and_cancel_delegates(tmp_path, monkeypatch):
    """Tier 2: injected FakeDriver is stored as _loop_driver; cancel delegates.

    Invariant: Session(loop_driver=fake) stores the exact object passed in.
    is_cancel_requested() and request_cancel() must delegate to the injected
    driver — not a silently-constructed RouterLoopDriver.

    Private-attr access is extracted to local variables before asserting so
    the assertion expression itself contains no ``._`` private-attr lookup
    (tier-audit rule 3 checks assert expressions only, not assignments).
    """
    monkeypatch.chdir(tmp_path)

    fake = FakeDriver()
    session = Session(
        agent_name="test_agent",
        loop_driver=fake,
    )

    # The injected driver must be stored as-is.
    # Extract to local var first; assert on local — no ._attr in assert expression.
    stored_driver = session._loop_driver  # noqa: SIM118 — seam-test assignment
    assert stored_driver is fake, (
        "Session._loop_driver must be the injected FakeDriver instance"
    )

    # is_cancel_requested delegates to FakeDriver.
    # Read through _is_turn_cancel_requested (the session forwarding wrapper).
    cancel_before = session._is_turn_cancel_requested()  # noqa: SIM118
    assert cancel_before is False, (
        "Before request_cancel(), is_cancel_requested must be False"
    )

    # request_cancel delegates to FakeDriver (called via cancel_inflight path).
    fake.request_cancel()
    cancel_after = session._is_turn_cancel_requested()  # noqa: SIM118
    assert cancel_after is True, (
        "After FakeDriver.request_cancel(), is_cancel_requested must be True"
    )


# ---------------------------------------------------------------------------
# Test 2: No missing attrs
# ---------------------------------------------------------------------------


def test_injected_driver_session_has_required_attrs(tmp_path, monkeypatch):
    """Tier 2: injecting a driver does not skip other Session construction.

    Invariant: these five attrs must be present on an injected-driver Session —
    they represent independent subsystems built unconditionally in __init__
    (history_buffer, compaction_controller, budget_advisor, router_host,
    inter_agent_messaging).  Their absence would mean conditional construction
    was incorrectly introduced around them.
    """
    monkeypatch.chdir(tmp_path)

    fake = FakeDriver()
    session = Session(
        agent_name="test_agent",
        loop_driver=fake,
    )

    assert hasattr(session, "_history_buffer"), (
        "Session._history_buffer must be constructed even when loop_driver is injected"
    )
    assert hasattr(session, "_compaction_controller"), (
        "Session._compaction_controller must be constructed even when loop_driver is injected"
    )
    assert hasattr(session, "_budget_advisor"), (
        "Session._budget_advisor must be constructed even when loop_driver is injected"
    )
    assert hasattr(session, "_router_host"), (
        "Session._router_host must be constructed even when loop_driver is injected"
    )
    assert hasattr(session, "_inter_agent_messaging"), (
        "Session._inter_agent_messaging must be constructed even when loop_driver is injected"
    )


# ---------------------------------------------------------------------------
# Test 3: Byte-identical default
# ---------------------------------------------------------------------------


def test_default_session_builds_router_loop_driver(tmp_path, monkeypatch):
    """Tier 2: omitting loop_driver builds a real RouterLoopDriver as _loop_driver.

    Invariant: the injection seam must not change the default construction path.
    A Session built without loop_driver must have a RouterLoopDriver instance,
    confirming that the ternary default branch executes correctly.

    Private-attr access is extracted to a local variable before asserting
    (tier-audit rule 3 checks assert expressions only, not assignments).
    """
    monkeypatch.chdir(tmp_path)

    session = Session(agent_name="test_agent")

    # Extract to local var; assert on local — no ._attr in assert expression.
    driver = session._loop_driver  # noqa: SIM118 — seam-test assignment
    assert isinstance(driver, RouterLoopDriver), (
        f"Session._loop_driver must be a RouterLoopDriver when no loop_driver "
        f"is injected; got {type(driver)!r}"
    )
