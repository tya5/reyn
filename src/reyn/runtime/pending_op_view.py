"""reyn.runtime.pending_op_view — the cross-channel pending-operation view.

``PendingOpView`` is the read-only, frozen value object describing one pending /
stalled operation in a channel-agnostic shape, so the TUI Pending tab and the
``/pending`` slash command can render it without depending on the originating
op type. Built from a ``UserIntervention`` today; the ``kind`` discriminator
admits other pending-op sources. Pure value object — no dependency on
``Session``.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from reyn.user_intervention import UserIntervention


@dataclass(frozen=True)
class PendingOpView:
    """Read-only view of a pending / stalled operation surfaced across channels
    (= issue #268 Phase 1, #270 umbrella vocabulary).

    First instance carries ``UserIntervention`` data; Phase B refactor
    (= #270) generalises this dataclass to also describe MCP pending
    calls / peer-delegation pending operations. The ``kind`` field is
    the discriminator. Field shape is **pinned at Phase A landing**
    per tui-coder commitment so the TUI Pending tab + ``/pending``
    slash command code path doesn't churn as new kinds land.

    Pinned fields (= TUI consume contract):
      - ``id``: stable identifier (= iv.id for interventions)
      - ``kind``: discriminator (= "intervention" for now, future
        "mcp_call" / "peer_delegate")
      - ``origin_channel_id``: where the op originated (= "tui:..." /
        "a2a:..." etc.)
      - ``created_at``: ISO timestamp string for age-rendering
      - ``summary``: short human-readable description (= iv.prompt
        first line for interventions)
      - ``detail``: optional second line (= iv.detail for interventions)
    """
    id: str
    kind: str
    origin_channel_id: str
    created_at: str
    summary: str
    detail: str = ""

    @classmethod
    def from_intervention(cls, iv: "UserIntervention") -> "PendingOpView":
        """Build a view from a ``UserIntervention``. ``created_at`` is
        the current time at view construction since iv doesn't carry
        its own timestamp; the TUI uses this for relative age display
        even though it's a view-time stamp.
        """
        from datetime import datetime, timezone  # noqa: PLC0415
        return cls(
            id=iv.id,
            kind="intervention",
            origin_channel_id=iv.origin_channel_id or "",
            created_at=datetime.now(timezone.utc).isoformat(),
            summary=iv.prompt,
            detail=iv.detail or "",
        )
