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
    """``tool_use:`` — the tool-use scheme per layer (#1593).

    Each layer (chat / step / phase) selects a registered ``ToolUseScheme`` by name,
    generalizing the binary ``action_retrieval.universal_wrappers_enabled`` toggle
    into a pluggable, per-layer scheme selector. #1657: the ``chat`` default is
    ``enumerate-all`` (the owner H1 fix — flat-listing actions stops
    invoke_action name-hallucination, 30%→100% non-hot-list tool-use). ``step`` /
    ``phase`` keep ``universal-category`` (unchanged — the H1 evidence is the chat
    path). Any layer can be set to another scheme name via reyn.yaml.
    """

    chat: str = "enumerate-all"
    step: str = "universal-category"
    phase: str = "universal-category"


def _build_tool_use_config(raw: object) -> ToolUseConfig:
    """Parse ``tool_use:`` from reyn.yaml. None / missing / empty → defaults
    (chat=enumerate-all #1657; step/phase=universal-category).

    Each layer key accepts a scheme name (string); a missing key keeps the default.
    A non-mapping block or non-string value is a config error (fail loud)."""
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
        step=_name("step", "universal-category"),
        phase=_name("phase", "universal-category"),
    )


