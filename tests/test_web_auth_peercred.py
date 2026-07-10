"""Tier 2: same-machine peer-credential identity for the UDS transport tier.

ADR-0039 P0 invariant 3: a UNIX-domain-socket connection carries an OS-verified
UID (the operator identity anchor for the T2 tier). The read is per-OS —
``SO_PEERCRED`` on Linux, ``getpeereid`` on macOS/BSD — so this pins the
abstraction against a REAL ``AF_UNIX`` socket pair: both ends live in the test
process, so the peer UID must equal the running user's UID. No mock — a real
kernel socket is the only thing that can prove the syscall wiring.
"""
from __future__ import annotations

import os
import socket
import sys

import pytest

from reyn.interfaces.web.auth import peer_uid_from_socket


@pytest.mark.skipif(not hasattr(socket, "AF_UNIX"), reason="AF_UNIX unavailable (Windows)")
def test_peer_uid_matches_running_user():
    """Tier 2: peer UID of an AF_UNIX socketpair equals the running user's UID."""
    a, b = socket.socketpair(socket.AF_UNIX)
    try:
        uid = peer_uid_from_socket(a)
    finally:
        a.close()
        b.close()
    # On a peer-cred-capable OS the read must return the real UID; on an
    # unsupported OS it degrades to None (caller falls back to token tier).
    if sys.platform.startswith("linux") or sys.platform == "darwin" or "bsd" in sys.platform:
        assert uid == os.getuid()
    else:
        assert uid is None


@pytest.mark.skipif(not hasattr(socket, "AF_UNIX"), reason="AF_UNIX unavailable (Windows)")
def test_peer_uid_is_readable_on_this_platform():
    """Tier 2: this dev/CI platform exposes a peer-cred mechanism (not None).

    Guards the per-OS abstraction: a regression that drops the macOS
    ``getpeereid`` branch (or the Linux ``SO_PEERCRED`` branch) would make the
    read return None on a platform that CAN resolve it — a silent loss of the
    UDS identity anchor.
    """
    if sys.platform not in ("darwin",) and not sys.platform.startswith("linux"):
        pytest.skip("peer-cred not expected on this platform")
    a, b = socket.socketpair(socket.AF_UNIX)
    try:
        assert peer_uid_from_socket(a) is not None
    finally:
        a.close()
        b.close()
