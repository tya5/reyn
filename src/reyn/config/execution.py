"""reyn.config.execution — execution config: Plan/TimeTravel/ToolUse. (#1682 #3 split)."""
from __future__ import annotations

import socket
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from reyn.config.chat import (  # #1682 #3: phase compaction config lives in chat
    PhaseActResultsCompactionConfig,
)
from reyn.runtime.budget.budget import CostConfig, CostLimitConfig


@dataclass
class ToolUseConfig:
    """``tool_use:`` — the chat-layer tool-use scheme selector (#1593; #2768).

    The ``chat`` layer selects a registered ``ToolUseScheme`` by name, generalizing
    the binary ``action_retrieval.universal_wrappers_enabled`` toggle into a
    pluggable scheme selector. #1657: the default is ``enumerate-all`` (the owner
    H1 fix — flat-listing actions stops invoke_action name-hallucination, 30%→100%
    non-hot-list tool-use). Set another scheme name (e.g. ``universal-category``)
    via reyn.yaml. #2768 removed the dead ``step`` / ``phase`` layers (phase-graph
    era — zero read sites; ``PhaseRouterLoopHost`` deleted in #2438).
    """

    chat: str = "enumerate-all"


def _build_tool_use_config(raw: object) -> ToolUseConfig:
    """Parse ``tool_use:`` from reyn.yaml. None / missing / empty → default
    (chat=enumerate-all #1657).

    The ``chat`` key accepts a scheme name (string); a missing key keeps the
    default. A non-mapping block or non-string value is a config error (fail loud)."""
    if raw is None:
        return ToolUseConfig()
    if not isinstance(raw, dict):
        raise ValueError(f"tool_use must be a mapping, got {type(raw).__name__}")

    def _name(key: str, default: str) -> str:
        if key not in raw:
            return default
        val = raw[key]
        if not isinstance(val, str) or not val:
            raise ValueError(
                f"tool_use.{key} must be a non-empty scheme name, got {val!r}"
            )
        return val

    return ToolUseConfig(
        chat=_name("chat", "enumerate-all"),  # #1657: owner default switch (H1 fix)
    )


