"""Tier 2: #4204 bucket A — `reyn web`'s UDS run dir anchors on the project
root, not raw cwd.

`_apply_auth_startup` built `run_dir` from bare `Path.cwd() / ".reyn" /
"run"` — runs unconditionally on every `reyn web` startup (the UDS socket
dir + TLS cert provisioning both live under it). Launched from a
subdirectory of the project, the socket dir would be created under a
phantom `.reyn/run/` instead of the real project's — a same-machine client
connecting via the documented `.reyn/run/` path would not find it there.

No mocks — real GatewayConfig/AuthConfig dataclasses, the real
`_apply_auth_startup` function, real on-disk directory creation.
"""
from __future__ import annotations

import argparse
from pathlib import Path

from reyn.config.media import AuthConfig, GatewayConfig
from reyn.config.root import ReynConfig
from reyn.interfaces.cli.commands.web import _apply_auth_startup
from tests._support.minimal_reyn_yaml import MINIMAL_REYN_YAML


def test_uds_run_dir_created_at_the_project_root_from_a_subdirectory(
    tmp_path: Path, monkeypatch,
) -> None:
    """Tier 2: #4204 — a UDS `reyn web` startup from a subdirectory of the
    project still creates the run dir (owner-only 0700, holding the
    socket) at the PROJECT root, not a phantom one under the subdirectory.

    UDS mode is the minimal path to isolate this (no token generation, no
    TLS provisioning branch to also stand up) — `_apply_auth_startup`
    returns early right after `run_dir.mkdir()` for a UDS bind."""
    (tmp_path / "reyn.yaml").write_text(MINIMAL_REYN_YAML, encoding="utf-8")
    subdir = tmp_path / "src" / "nested"
    subdir.mkdir(parents=True)
    monkeypatch.chdir(subdir)

    uds_path = tmp_path / "reyn.sock"
    args = argparse.Namespace(host="127.0.0.1", uds=str(uds_path), port=0)
    config = ReynConfig(gateway=GatewayConfig(auth=AuthConfig()))

    _apply_auth_startup(args, config)

    real_run_dir = tmp_path / ".reyn" / "run"
    phantom_run_dir = subdir / ".reyn" / "run"
    assert real_run_dir.is_dir()
    assert not phantom_run_dir.exists()
