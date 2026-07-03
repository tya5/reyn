"""Tier 2: OS invariant — #2259 config-recovery emission for the index-sources registry.

The REAL ``index_drop`` op handler — handed a ``state_log`` via its OpContext (the
production wiring: session → ToolContext → drop_source adapter → OpContext) — records
a full-state config GENERATION carrying the FULL post-drop sources registry content
after the SourceManifest persists ``.reyn/config/index/sources.yaml``. The yaml is a
derived projection; the generation is the recovery truth (reconstructable as-of-cut).

Real StateLog + real op handler + real SourceManifest + real AgentRegistry + on-disk yaml.
"""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from reyn.core.events.state_log import StateLog
from reyn.core.op_runtime.context import OpContext
from reyn.core.op_runtime.index_drop import handle as drop_handle
from reyn.data.index.source_manifest import SourceEntry, get_source_manifest
from reyn.runtime.registry import AgentRegistry
from reyn.schemas.models import IndexDropIROp


class _Events:
    subscribers: list = []

    def emit(self, *_a, **_k) -> None:
        pass


class _Workspace:
    def __init__(self, base_dir: Path) -> None:
        self.base_dir = base_dir


def _no_factory(_profile):
    raise AssertionError("session factory must not be called in these tests")


@pytest.mark.asyncio
async def test_index_drop_records_generation_full_sources(tmp_path):
    """Tier 2: a REAL index_drop op (state_log threaded into OpContext) records a generation
    carrying the FULL post-drop sources registry (the dropped source absent, the surviving one
    present) — reconstructable as-of-cut. RED if the op didn't record after the manifest
    persisted sources.yaml."""
    state_log = StateLog(tmp_path / ".reyn" / "wal.jsonl")

    # Seed the real manifest with two sources (persists .reyn/index/sources.yaml).
    manifest = get_source_manifest(tmp_path)
    await manifest.upsert(
        SourceEntry(name="docs", description="user docs", path=".reyn/memory/*.md")
    )
    await manifest.upsert(
        SourceEntry(name="code", description="python", path="src/**/*.py")
    )

    ctx = OpContext(
        workspace=_Workspace(base_dir=tmp_path),
        events=_Events(),
        permission_decl=None,
        permission_resolver=None,  # no gate in this unit-test context
        skill_name="test",
        intervention_bus=None,
        subscribers=[],
        state_log=state_log,
    )
    op = IndexDropIROp(kind="index_drop", source="code")
    result = await drop_handle(op=op, ctx=ctx)
    assert result["removed"] is True

    # the op recorded a generation → reconstruct as-of the current head re-materialises the
    # FULL post-drop sources registry onto disk (the recovery truth).
    reg = AgentRegistry(
        project_root=tmp_path, session_factory=_no_factory, state_log=state_log,
    )
    reg._reconcile_config_as_of_cut(state_log.current_seq)
    content = yaml.safe_load(
        (tmp_path / ".reyn" / "config" / "index" / "sources.yaml").read_text(encoding="utf-8")
    )
    assert set(content) == {"docs"}
    assert content["docs"]["description"] == "user docs"
