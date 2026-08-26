"""Tier 2: #5100 preserves malformed-vs-absent hooks YAML status."""
from __future__ import annotations

from pathlib import Path

from reyn.config.loader import HookYamlReadError, read_and_expand_hooks_yaml


def test_malformed_hooks_yaml_is_distinguishable_from_absent_hooks_yaml(tmp_path: Path) -> None:
    """Tier 2: malformed existing YAML raises a typed read error, while an absent file returns None."""
    malformed = tmp_path / "malformed.yaml"
    malformed.write_text("hooks: [turn_end\n", encoding="utf-8")

    try:
        read_and_expand_hooks_yaml(malformed, agent_name="alice", project_root=tmp_path)
    except HookYamlReadError:
        pass
    else:
        raise AssertionError("malformed hooks YAML must preserve a read failure")

    assert read_and_expand_hooks_yaml(
        tmp_path / "absent.yaml", agent_name="alice", project_root=tmp_path,
    ) is None
