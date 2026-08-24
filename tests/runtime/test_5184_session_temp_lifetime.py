"""Tier 2: #5184 session-owned child temp lifetime."""
from __future__ import annotations

import tempfile
from pathlib import Path

from reyn.core.events.state_log import StateLog
from tests._support.agent_session import make_session


def test_constructing_a_session_does_not_create_child_temp_dir(tmp_path: Path) -> None:
    """Tier 2: construction alone leaves no session child-temp artifact."""
    session_id = "construct-only-5184"
    temp_dir = Path(tempfile.gettempdir()) / "reyn" / "test-agent" / session_id
    if temp_dir.exists():
        import shutil

        shutil.rmtree(temp_dir)

    session = make_session(
        agent_name="test-agent",
        session_id=session_id,
        state_log=StateLog(tmp_path / "state.wal"),
        snapshot_path=tmp_path / ".reyn" / "agents" / "test-agent" / "state" / "snapshot.json",
    )

    assert not temp_dir.exists()
    session.router_host.make_router_op_context()
    assert temp_dir.is_dir()

    import shutil

    shutil.rmtree(temp_dir)
