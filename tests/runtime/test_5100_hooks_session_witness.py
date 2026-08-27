"""Tier 2: #5100 malformed per-session hooks reach the existing warning seam."""
from __future__ import annotations

from pathlib import Path

from tests._support.agent_session import make_session


def test_session_reads_malformed_per_session_hooks_and_records_location(tmp_path: Path) -> None:
    """Tier 2: a real Session reads malformed YAML, records only file and location, and replaces on reread."""
    session = make_session(
        agent_name="hooks-warning",
        workspace_base_dir=tmp_path,
        workspace_state_dir=tmp_path / "state",
    )
    path = session._hooks_yaml_layers()[1][1]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("hooks: [turn_end\n", encoding="utf-8")

    assert session._read_per_session_hooks() == []
    warnings = session.hooks_config_warnings
    assert warnings == [f"hooks.yaml could not be read: {path.name} (line 2, column 1)"]
    assert "turn_end" not in warnings[0]

    assert session._read_per_session_hooks() == []
    assert session.hooks_config_warnings == warnings


def test_healthy_per_session_hooks_have_no_warning(tmp_path: Path) -> None:
    """Tier 2: a valid per-session hooks file does not create a warning."""
    session = make_session(
        agent_name="healthy-hooks",
        workspace_base_dir=tmp_path,
        workspace_state_dir=tmp_path / "state",
    )
    path = session._hooks_yaml_layers()[1][1]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("hooks: []\n", encoding="utf-8")

    assert session._read_per_session_hooks() == []
    assert session.hooks_config_warnings == []
