"""Tier 2: #1212 PR2 — `tool_calls_op_loop_skills` opt-in loads from reyn.yaml.

The native-tools op-loop is rolled out per-skill via a config opt-in list (the
P-clean rollout flag: the OS decides the execution mechanism, config carries skill
*names* as data, Phase frontmatter is unchanged). This pins the load-from-disk
path so the field is actually wired through `load_config` (not just a dataclass
default), using a NON-DEFAULT value so an unwired/aliased field can't pass
trivially.
"""
from __future__ import annotations

from pathlib import Path

import yaml

from reyn.config import load_config


def _write_reyn_yaml(path: Path, content: dict) -> None:
    path.write_text(
        yaml.dump(content, allow_unicode=True, default_flow_style=False),
        encoding="utf-8",
    )


def test_op_loop_skills_loaded_from_reyn_yaml(tmp_path: Path) -> None:
    """Tier 2: a non-default opt-in list in reyn.yaml is parsed onto ReynConfig."""
    _write_reyn_yaml(
        tmp_path / "reyn.yaml",
        {"tool_calls_op_loop_skills": ["swe_bench", "eval"]},
    )
    cfg = load_config(tmp_path)
    assert cfg.tool_calls_op_loop_skills == ["swe_bench", "eval"]


def test_op_loop_skills_default_empty_when_absent(tmp_path: Path) -> None:
    """Tier 2: absent from reyn.yaml → empty list (json-mode default for every skill)."""
    _write_reyn_yaml(tmp_path / "reyn.yaml", {"model": "standard"})
    cfg = load_config(tmp_path)
    assert cfg.tool_calls_op_loop_skills == []
