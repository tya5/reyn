"""Tier 2: #5184 child-process temp source invariant."""
from __future__ import annotations

from pathlib import Path

import pytest

from reyn.security.sandbox.policy import SandboxPolicy, resolve_passthrough_env


def test_session_temp_source_requires_a_real_writable_directory(tmp_path: Path) -> None:
    """Tier 2: session-owned child policy supplies TMPDIR from an existing directory."""
    policy = SandboxPolicy(temp_dir=str(tmp_path), temp_source="session")
    env = resolve_passthrough_env(policy)
    assert env["TMPDIR"] == str(tmp_path)


def test_session_temp_source_without_a_directory_is_rejected(tmp_path: Path) -> None:
    """Tier 2: a spawning policy without writable temp fails before child launch."""
    policy = SandboxPolicy(temp_dir=str(tmp_path / "missing"), temp_source="session")
    with pytest.raises(ValueError, match="not writable"):
        resolve_passthrough_env(policy)
