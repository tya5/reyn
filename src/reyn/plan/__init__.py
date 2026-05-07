"""Plan-mode runtime substrate (ADR-0023 Phase 2).

Parallel infrastructure to ``src/reyn/skill/`` for plan-mode resume.
Step 1 (decomposition artifact helpers) is the standalone foundation;
later steps add ``PlanSnapshot``, ``PlanRegistry``, ``PlanRuntime``,
analyzer, coordinator.
"""
from __future__ import annotations

from reyn.plan.decomposition import (
    DECOMPOSITION_SCHEMA_VERSION,
    DecompositionCorruptError,
    decomposition_dir,
    decomposition_path,
    delete_decomposition,
    read_decomposition,
    write_decomposition,
)

__all__ = [
    "DECOMPOSITION_SCHEMA_VERSION",
    "DecompositionCorruptError",
    "decomposition_dir",
    "decomposition_path",
    "delete_decomposition",
    "read_decomposition",
    "write_decomposition",
]
