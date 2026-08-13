"""Tier 2: /reset confirm-prompt includes docs link.

Covers the surface added in Wave-12 T2-5b (A#6):
- /reset confirm-prompt body includes a reference to
  crash-recovery-and-resume.md so users know where to learn about
  what snapshots+WAL hold before confirming a destructive reset.

Policy compliance:
- No MagicMock / AsyncMock / patch — real instances throughout.
- Docstring first line declares the Tier.
- Uses only public surfaces: reset_cmd handler + the client transport's
  recorded display (#3595 S4 routes a slash reply through that seam).
"""
from __future__ import annotations

import asyncio
import sys

from tests._support.paths import REPO_ROOT

_SRC = REPO_ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from tests._support.slash import slash_ctx  # noqa: E402 — after the sys.path bootstrap


def test_reset_confirm_includes_docs_path() -> None:
    """Tier 2: /reset (without args) confirm-prompt includes crash-recovery-and-resume.md."""
    from reyn.interfaces.slash import REGISTRY  # noqa: F401 — triggers registration
    from reyn.interfaces.slash.reset import reset_cmd

    ctx = slash_ctx()
    asyncio.run(reset_cmd(ctx, ""))

    assert ctx.transport.displayed, "expected at least one reply from /reset"
    combined = "\n".join(ctx.transport.texts())
    assert "crash-recovery-and-resume.md" in combined, (
        f"Expected docs link in /reset prompt, got: {combined!r}"
    )
