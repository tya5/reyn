"""Tier 2: #5226 — ``reyn doctor``'s process-registry section
(``_print_process_registry``), the read surface lead-coder ruled for
(``reyn doctor``, not a new ``reyn ps`` subcommand — see
``process_registry.py``'s own module docstring for the full incident and
design this closes).

Real ``process_registry`` module (``register_process``/``live_processes``),
``PROCESSES_DIR`` monkeypatched to a ``tmp_path`` — the real
``~/.reyn/processes/`` is shared, user-global state a test must never
touch. ``capsys`` captures the real ``print()`` calls the function makes —
no mocks, mirrors ``test_4364_c4_doctor_model_reachability.py``'s own
pattern for a single doctor slice."""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from reyn.interfaces.cli.commands.doctor import _print_process_registry
from reyn.runtime import process_registry


@pytest.fixture(autouse=True)
def _isolated_processes_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    processes_dir = tmp_path / "processes"
    monkeypatch.setattr(process_registry, "PROCESSES_DIR", processes_dir)
    return processes_dir


def test_no_registered_processes_says_so_plainly(capsys: pytest.CaptureFixture) -> None:
    """Tier 2: falsification contrast — an empty registry (nothing ever
    registered, or PROCESSES_DIR doesn't exist) prints a plain "none
    found" line, never a fabricated "0 processes" framed as a real count
    of something that was actually measured."""
    _print_process_registry()
    out = capsys.readouterr().out
    assert "no reyn process markers found" in out


def test_a_real_registered_process_is_reported_by_pid_ppid_cwd_subcommand(
    capsys: pytest.CaptureFixture,
) -> None:
    """Tier 2: acceptance — this test process's OWN real marker (written
    via the real ``register_process``) is reported with its real pid,
    ppid, cwd, and the subcommand string it was registered with."""
    process_registry.register_process("chat")
    try:
        _print_process_registry()
        out = capsys.readouterr().out

        assert "1 process(es) currently alive" in out
        assert f"pid={os.getpid()}" in out
        assert f"ppid={os.getppid()}" in out
        assert os.getcwd() in out
        assert "subcommand: chat" in out
    finally:
        process_registry._cleanup(os.getpid())
