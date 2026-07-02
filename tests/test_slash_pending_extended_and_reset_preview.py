"""Tier 2: /pending list — needs-attention gate.

Pinned:
  1. /pending list with no stuck/pending items → no "needs attention" section.

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

from reyn.runtime.outbox import OutboxMessage  # noqa: E402

# ── Shared stubs ──────────────────────────────────────────────────────────


class _StubSession:
    """Minimal session stub supporting /pending list."""

    def __init__(
        self,
        *,
        pending_ops: list | None = None,
    ) -> None:
        self._pending_ops = pending_ops or []
        self.outbox_messages: list[OutboxMessage] = []

    async def _put_outbox(self, msg: OutboxMessage) -> None:
        self.outbox_messages.append(msg)

    def list_stalled_interventions(self) -> list:
        return list(self._pending_ops)

    def captured_text(self) -> str:
        """Concatenate all outbox message texts for assertion convenience."""
        return "\n".join(m.text for m in self.outbox_messages)


# ── /pending list tests ───────────────────────────────────────────────────


def test_pending_list_no_attention_when_clean() -> None:
    """Tier 2: /pending list with 0 interrupted/stuck → no needs-attention section."""
    from reyn.interfaces.slash import REGISTRY  # noqa: F401 — triggers registration
    from reyn.interfaces.slash.pending import pending_cmd

    session = _StubSession()
    asyncio.run(pending_cmd(session, "list"))

    text = session.captured_text()
    assert "needs attention" not in text, (
        f"Expected no 'needs attention' section, got: {text!r}"
    )


