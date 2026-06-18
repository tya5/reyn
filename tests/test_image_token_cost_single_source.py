"""Tier 2: OS invariant — the image fixed-token cost has a single source.

#1171 media-cap follow-up: the per-image prompt token cost (1024) was duplicated
as three module-level constants (compaction engine, router_loop, read_tool_result).
They are now single-sourced from ``engine._IMAGE_FIXED_TOKEN_COST`` so the media
per-turn bound, the load-contract error, and the compaction estimate can never
drift apart. This pins the invariant: re-hardcoding the literal in any consumer
(instead of referencing the engine constant) regresses here.
"""
from __future__ import annotations

from reyn.runtime.router_loop import _MEDIA_IMAGE_TOKEN_COST
from reyn.services.compaction.engine import _IMAGE_FIXED_TOKEN_COST


def test_image_token_cost_is_single_sourced() -> None:
    """Tier 2: all image-token-cost consumers reference the one engine constant.

    (#1449: read_tool_result's _MEDIA_REF_IMAGE_TOKEN_COST — a former third
    consumer — was retired with the tool.)
    """
    assert _MEDIA_IMAGE_TOKEN_COST == _IMAGE_FIXED_TOKEN_COST, (
        "router_loop._MEDIA_IMAGE_TOKEN_COST must derive from "
        "engine._IMAGE_FIXED_TOKEN_COST, not a re-hardcoded literal"
    )
