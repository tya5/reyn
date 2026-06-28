"""Tier 2: OS invariant — #2248 PR-A2 config-recovery emission for the index-sources registry.

The REAL ``index_drop`` op handler — handed a ``state_log`` via its OpContext (the
production wiring: session → ToolContext → drop_source adapter → OpContext) — emits
a ``config_changed`` WAL event carrying the FULL post-drop sources registry content
after the SourceManifest persists ``.reyn/index/sources.yaml``. The yaml is a derived
projection; the WAL event is the recovery truth.

Real StateLog + real op handler + real SourceManifest + on-disk yaml (no mocks).
"""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from reyn.core.events.state_log import StateLog
from reyn.core.op_runtime.context import OpContext
from reyn.core.op_runtime.index_drop import handle as drop_handle
from reyn.data.index.source_manifest import SourceEntry, get_source_manifest
from reyn.schemas.models import IndexDropIROp


class _Events:
    subscribers: list = []

    def emit(self, *_a, **_k) -> None:
        pass


class _Workspace:
    def __init__(self, base_dir: Path) -> None:
        self.base_dir = base_dir


@pytest.mark.asyncio
async def test_index_drop_emits_config_changed_full_sources(tmp_path):
    """Tier 2: a REAL index_drop op (state_log threaded into OpContext) emits
    config_changed carrying the FULL post-drop sources registry (the dropped source
    absent, the surviving one present) keyed by the `.reyn`-relative path. RED if the
    op didn't emit after the manifest persisted sources.yaml."""
    state_log = StateLog(tmp_path / ".reyn" / "wal.jsonl")

    # Seed the real manifest with two sources (persists .reyn/index/sources.yaml).
    manifest = get_source_manifest(tmp_path)
    await manifest.upsert(
        SourceEntry(name="docs", description="user docs", path=".reyn/memory/*.md")
    )
    await manifest.upsert(
        SourceEntry(name="code", description="python", path="src/**/*.py")
    )
    before = state_log.current_seq

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
    result = await drop_handle(op=op, ctx=ctx, caller="control_ir")
    assert result["removed"] is True

    [ev] = [
        e
        for e in state_log.iter_from(before + 1)
        if e.get("kind") == "config_changed"
    ]
    assert ev["path"] == "config/index/sources.yaml"
    assert set(ev["content"]) == {"docs"}
    assert ev["content"]["docs"]["description"] == "user docs"
    # the yaml is a derived projection of the same content
    on_disk = yaml.safe_load(
        (tmp_path / ".reyn" / "config" / "index" / "sources.yaml").read_text(encoding="utf-8")
    )
    assert ev["content"] == on_disk
