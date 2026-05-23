"""Tier 2: /reset confirm-prompt includes docs link.

Covers the surface added in Wave-12 T2-5b (A#6):
- /reset confirm-prompt body includes a reference to
  crash-recovery-and-resume.md so users know where to learn about
  what snapshots+WAL hold before confirming a destructive reset.

Policy compliance:
- No MagicMock / AsyncMock / patch — real instances throughout.
- Docstring first line declares the Tier.
- Uses only public surfaces: reset_cmd handler + captured reply text.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

_SRC = Path(__file__).parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))


class _FakeSession:
    """Minimal session double that captures reply text via _put_outbox."""

    def __init__(self) -> None:
        self.messages: list[str] = []

    async def _put_outbox(self, msg: object) -> None:
        self.messages.append(getattr(msg, "text", str(msg)))


def test_reset_confirm_includes_docs_path() -> None:
    """Tier 2: /reset (without args) confirm-prompt includes crash-recovery-and-resume.md."""
    from reyn.chat.slash import REGISTRY  # noqa: F401 — triggers registration
    from reyn.chat.slash.reset import reset_cmd

    session = _FakeSession()
    asyncio.run(reset_cmd(session, ""))

    assert session.messages, "expected at least one reply from /reset"
    combined = "\n".join(session.messages)
    assert "crash-recovery-and-resume.md" in combined, (
        f"Expected docs link in /reset prompt, got: {combined!r}"
    )
