"""Tier 2: /pending list needs-attention + /reset confirm preview.

Pinned:
  1. /pending list with 0 interrupted plans + 0 stuck skills
     → no "needs attention" section in output.
  2. /pending list with 1 interrupted plan → output contains "interrupted".
  3. /pending list with 1 stuck skill → output contains "stuck @".
  4. /reset (no arg, no state) → confirm output contains "Currently:" with
     "0 skills" and "0 plans".
  5. /reset (no arg, with state: 2 skills, 1 plan) → output
     contains "2 skills" and "1 plan".

Note: error_box_count was removed from the state summary in the inline
scroll-away refactor (errors are now plain log lines, not persistent widgets).

Policy compliance:
  - No MagicMock / AsyncMock / patch — stub session pattern only.
  - Docstring first lines declare the Tier.
  - Assertions on public surface (captured outbox text).
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

_SRC = Path(__file__).parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from reyn.chat.outbox import OutboxMessage  # noqa: E402

# ── Shared stubs ──────────────────────────────────────────────────────────


class _StubSession:
    """Minimal session stub supporting /pending list and /reset confirm preview."""

    def __init__(
        self,
        *,
        interrupted_plans: list[dict] | None = None,
        stuck_skills: list[dict] | None = None,
        running_skills: int = 0,
        running_plans: int = 0,
        pending_ops: list | None = None,
    ) -> None:
        self._interrupted_plans = interrupted_plans or []
        self._stuck_skills = stuck_skills or []
        self._running_skills = running_skills
        self._running_plans = running_plans
        self._pending_ops = pending_ops or []
        self.outbox_messages: list[OutboxMessage] = []

    async def _put_outbox(self, msg: OutboxMessage) -> None:
        self.outbox_messages.append(msg)

    def list_stalled_interventions(self) -> list:
        return list(self._pending_ops)

    def current_state_summary(self) -> dict:
        return {
            "running_skills": self._running_skills,
            "running_plans": self._running_plans,
            "interrupted_plans": list(self._interrupted_plans),
            "stuck_skills": list(self._stuck_skills),
        }

    def captured_text(self) -> str:
        """Concatenate all outbox message texts for assertion convenience."""
        return "\n".join(m.text for m in self.outbox_messages)


# ── /pending list tests ───────────────────────────────────────────────────


def test_pending_list_no_attention_when_clean() -> None:
    """Tier 2: /pending list with 0 interrupted/stuck → no needs-attention section."""
    from reyn.chat.slash import REGISTRY  # noqa: F401 — triggers registration
    from reyn.chat.slash.pending import pending_cmd

    session = _StubSession(
        interrupted_plans=[],
        stuck_skills=[],
    )
    asyncio.run(pending_cmd(session, "list"))

    text = session.captured_text()
    assert "needs attention" not in text, (
        f"Expected no 'needs attention' section, got: {text!r}"
    )


def test_pending_list_shows_interrupted_plan() -> None:
    """Tier 2: /pending list with 1 interrupted plan → output contains 'interrupted'."""
    from reyn.chat.slash import REGISTRY  # noqa: F401
    from reyn.chat.slash.pending import pending_cmd

    session = _StubSession(
        interrupted_plans=[{
            "plan_id": "abc12345",
            "goal": "write tests",
            "exc_type": "KeyboardInterrupt",
            "n_completed": 2,
            "n_total": 5,
        }],
    )
    asyncio.run(pending_cmd(session, "list"))

    text = session.captured_text()
    assert "needs attention" in text, (
        f"Expected 'needs attention' in output, got: {text!r}"
    )
    assert "interrupted" in text, (
        f"Expected 'interrupted' in output, got: {text!r}"
    )


def test_pending_list_shows_stuck_skill() -> None:
    """Tier 2: /pending list with 1 stuck skill → output contains 'stuck @'."""
    from reyn.chat.slash import REGISTRY  # noqa: F401
    from reyn.chat.slash.pending import pending_cmd

    session = _StubSession(
        stuck_skills=[{
            "skill_name": "foo",
            "run_id": "abcdefgh",
            "stuck_at": "llm_called",
        }],
    )
    asyncio.run(pending_cmd(session, "list"))

    text = session.captured_text()
    assert "needs attention" in text, (
        f"Expected 'needs attention' in output, got: {text!r}"
    )
    assert "stuck @" in text, (
        f"Expected 'stuck @' in output, got: {text!r}"
    )


# ── /reset confirm preview tests ─────────────────────────────────────────


def test_reset_no_arg_no_state_shows_currently_zero() -> None:
    """Tier 2: /reset (no arg, no state) → contains 'Currently:' with zeros."""
    from reyn.chat.slash import REGISTRY  # noqa: F401
    from reyn.chat.slash.reset import reset_cmd

    session = _StubSession(
        running_skills=0,
        running_plans=0,
    )
    asyncio.run(reset_cmd(session, ""))

    text = session.captured_text()
    assert "Currently:" in text, (
        f"Expected 'Currently:' in /reset prompt, got: {text!r}"
    )
    assert "0 skills" in text, (
        f"Expected '0 skills' in /reset prompt, got: {text!r}"
    )
    assert "0 plans" in text, (
        f"Expected '0 plans' in /reset prompt, got: {text!r}"
    )


def test_reset_no_arg_with_state_shows_counts() -> None:
    """Tier 2: /reset (no arg, 2 skills, 1 plan) → output contains counts."""
    from reyn.chat.slash import REGISTRY  # noqa: F401
    from reyn.chat.slash.reset import reset_cmd

    session = _StubSession(
        running_skills=2,
        running_plans=1,
    )
    asyncio.run(reset_cmd(session, ""))

    text = session.captured_text()
    assert "2 skills" in text, (
        f"Expected '2 skills' in /reset prompt, got: {text!r}"
    )
    assert "1 plan" in text, (
        f"Expected '1 plan' in /reset prompt, got: {text!r}"
    )
