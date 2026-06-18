"""Tier 2 — reyn.api.safe.process contract tests (FP-0042 Phase 2.2).

The :mod:`reyn.api.safe.process` module exposes ``getpid`` + ``pid_alive``
to safe-mode python steps without a permission gate (process identity
is ambient in the same sense as ``time`` / ``random``). These tests
pin: getpid returns the same value as ``os.getpid``, pid_alive is
True for the current process, False for an obviously-dead PID, and
False for non-positive PIDs (= input guard).
"""
from __future__ import annotations

import os

from reyn.api.safe import process as sp


def test_getpid_matches_os_getpid() -> None:
    """Tier 2: reyn.api.safe.process.getpid() returns os.getpid() verbatim."""
    assert sp.getpid() == os.getpid()


def test_pid_alive_true_for_self() -> None:
    """Tier 2: pid_alive returns True for the current process."""
    assert sp.pid_alive(os.getpid()) is True


def test_pid_alive_false_for_obviously_dead_pid() -> None:
    """Tier 2: pid_alive returns False for a PID very unlikely to exist.

    Linux/macOS PIDs are bounded by ``kernel.pid_max`` (= ~32k on macOS,
    ~4M on modern Linux). 2_000_000_000 is comfortably past both, so
    ``os.kill(pid, 0)`` raises ``ProcessLookupError`` and pid_alive
    returns False.
    """
    assert sp.pid_alive(2_000_000_000) is False


def test_pid_alive_false_for_zero() -> None:
    """Tier 2: pid_alive(0) returns False (= guarded against the kill(0,0)
    "signal all processes in current group" semantics, which would
    incorrectly return True).
    """
    assert sp.pid_alive(0) is False


def test_pid_alive_false_for_negative() -> None:
    """Tier 2: pid_alive on a negative PID returns False (= guarded against
    kill(-1, 0) "broadcast" semantics)."""
    assert sp.pid_alive(-1) is False
    assert sp.pid_alive(-12345) is False
