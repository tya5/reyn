"""Tier 2: OS invariant — #2248 PR-A2 config-recovery emission for the hooks registry.

The REAL ``_handle_hooks_add`` handler — handed a ``state_log`` via its ToolContext
(the production wiring: session → ToolContext) — emits a ``config_changed`` WAL
event carrying the FULL post-mutation hooks registry content after it persists
``.reyn/hooks.yaml``. The yaml is a derived projection; the WAL event is the
recovery truth.

Real StateLog + real handler + on-disk yaml (no mocks).
"""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from reyn.core.events.state_log import StateLog
from reyn.tools.hooks import _handle_hooks_add
from reyn.tools.types import ToolContext


class _Events:
    subscribers: list = []

    def emit(self, *_a, **_k) -> None:
        pass


class _Workspace:
    def __init__(self, base_dir: Path) -> None:
        self.base_dir = base_dir


def _ctx(base_dir: Path, state_log: StateLog) -> ToolContext:
    return ToolContext(
        events=_Events(),
        permission_resolver=None,  # unit-test context → _gate is a no-op
        workspace=_Workspace(base_dir=base_dir),
        caller_kind="router",
        state_log=state_log,
    )


@pytest.mark.asyncio
async def test_hooks_add_emits_config_changed_full_content(tmp_path):
    """Tier 2: a REAL hooks_add (state_log threaded into ToolContext) emits
    config_changed with the FULL post-add hooks content keyed by the
    `.reyn`-relative path. RED if the handler didn't emit after persisting."""
    state_log = StateLog(tmp_path / ".reyn" / "wal.jsonl")
    ctx = _ctx(tmp_path, state_log)
    before = state_log.current_seq

    result = await _handle_hooks_add(
        {"on": "turn_end", "message": "keep going", "wake": True, "name": "loop"},
        ctx,
    )
    assert result["status"] == "ok"
    assert result["added"] is True

    [ev] = [
        e
        for e in state_log.iter_from(before + 1)
        if e.get("kind") == "config_changed"
    ]
    assert ev["path"] == "config/hooks.yaml"
    hooks = ev["content"]["hooks"]
    [hook] = [h for h in hooks if h.get("name") == "loop"]
    assert hook["on"] == "turn_end"
    assert hook["template_push"]["message"] == "keep going"
    # the yaml is a derived projection of the same content
    on_disk = yaml.safe_load(
        (tmp_path / ".reyn" / "config" / "hooks.yaml").read_text(encoding="utf-8")
    )
    assert ev["content"] == on_disk
