"""Tier 2: OS invariant — #2259 config-recovery emission for the hooks registry.

The REAL ``_handle_hooks_add`` handler — handed a ``state_log`` via its ToolContext
(the production wiring: session → ToolContext) — records a full-state config GENERATION
carrying the FULL post-mutation hooks registry content after it persists
``.reyn/config/hooks.yaml``. The yaml is a derived projection; the generation is the
recovery truth (it reconstructs the registry as-of-cut and survives WAL truncation).

Real StateLog + real handler + real AgentRegistry + on-disk yaml (no mocks).
"""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from reyn.core.events.state_log import StateLog
from reyn.runtime.registry import AgentRegistry
from reyn.tools.hooks import _handle_hooks_add
from reyn.tools.types import ToolContext


class _Events:
    subscribers: list = []

    def emit(self, *_a, **_k) -> None:
        pass


class _Workspace:
    def __init__(self, base_dir: Path) -> None:
        self.base_dir = base_dir


def _no_factory(_profile):
    raise AssertionError("session factory must not be called in these tests")


def _ctx(base_dir: Path, state_log: StateLog) -> ToolContext:
    return ToolContext(
        events=_Events(),
        permission_resolver=None,  # unit-test context → _gate is a no-op
        workspace=_Workspace(base_dir=base_dir),
        caller_kind="router",
        state_log=state_log,
    )


@pytest.mark.asyncio
async def test_hooks_add_records_generation_full_content(tmp_path):
    """Tier 2: a REAL hooks_add (state_log threaded into ToolContext) records a generation with
    the FULL post-add hooks content — reconstructable as-of-cut. RED if the handler didn't record
    after persisting."""
    state_log = StateLog(tmp_path / ".reyn" / "wal.jsonl")
    ctx = _ctx(tmp_path, state_log)

    result = await _handle_hooks_add(
        {"on": "turn_end", "message": "keep going", "wake": True, "name": "loop"},
        ctx,
    )
    assert result["status"] == "ok"
    assert result["added"] is True

    # reconstruct from the generation as-of the current head — the recovery truth onto disk.
    reg = AgentRegistry(
        project_root=tmp_path, session_factory=_no_factory, state_log=state_log,
    )
    reg._reconcile_config_as_of_cut(state_log.current_seq)
    content = yaml.safe_load(
        (tmp_path / ".reyn" / "config" / "hooks.yaml").read_text(encoding="utf-8")
    )
    [hook] = [h for h in content["hooks"] if h.get("name") == "loop"]
    assert hook["on"] == "turn_end"
    assert hook["template_push"]["message"] == "keep going"
