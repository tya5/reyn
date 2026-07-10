"""Per-OS peer-credential resolution for a UNIX-domain socket connection.

A same-machine cross-process connection (the T2 tier of the thin-client
transport model) carries an OS-verified identity: the kernel knows the UID of
the process on the other end of a ``AF_UNIX`` socket. Reading that UID needs a
**different syscall per OS** — there is no single portable API — so this module
abstracts the three cases behind one function:

  - **Linux**: ``getsockopt(SOL_SOCKET, SO_PEERCRED)`` returns a ``struct
    ucred`` (pid, uid, gid).
  - **macOS / *BSD**: ``SO_PEERCRED`` does not exist; the equivalent is the
    libc ``getpeereid(fd, uid_t*, gid_t*)`` call (reached here via ``ctypes``).
  - **Windows / anything without ``AF_UNIX`` peer-cred**: returns ``None`` — the
    caller falls back to the loopback-plus-token tier instead.

The UID this returns is the authorization anchor: the auth layer compares it
against the server-owner UID so only the operator's own processes are admitted
on the UDS tier (defense in depth behind the socket's ``0600`` file mode). It is
deliberately a small, socket-in / uid-out function so it can be exercised
directly against a real ``socket.socketpair(AF_UNIX)`` — both ends live in the
test process, so the peer UID is the running user's own UID.
"""
from __future__ import annotations

import socket
import struct
import sys


def peer_uid_from_socket(sock: socket.socket) -> int | None:
    """Return the OS-verified UID of the peer on *sock*, or ``None``.

    *sock* must be a connected ``AF_UNIX`` stream socket. ``None`` is returned
    when the running OS exposes no peer-credential mechanism (e.g. Windows) or
    the syscall fails — the caller treats ``None`` as "peer UID unknown" and
    relies on the token tier / socket file mode instead of guessing.
    """
    try:
        if sys.platform.startswith("linux") and hasattr(socket, "SO_PEERCRED"):
            fmt = "3i"  # struct ucred { pid_t pid; uid_t uid; gid_t gid; }
            raw = sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize(fmt))
            _pid, uid, _gid = struct.unpack(fmt, raw)
            return int(uid)
        if sys.platform == "darwin" or "bsd" in sys.platform:
            return _getpeereid_uid(sock)
    except OSError:
        return None
    return None


def _getpeereid_uid(sock: socket.socket) -> int | None:
    """macOS / *BSD peer UID via libc ``getpeereid`` (``LOCAL_PEERCRED`` family).

    ``socket`` has no ``SO_PEERCRED`` on these platforms; ``getpeereid`` is the
    documented equivalent. Reached via ``ctypes`` because CPython's ``socket``
    module does not wrap it. Returns ``None`` on any failure (missing symbol,
    non-zero return) so the caller degrades to the token tier.
    """
    import ctypes
    import ctypes.util

    libc_name = ctypes.util.find_library("c")
    if libc_name is None:
        return None
    libc = ctypes.CDLL(libc_name, use_errno=True)
    getpeereid = getattr(libc, "getpeereid", None)
    if getpeereid is None:
        return None
    uid = ctypes.c_uint32()
    gid = ctypes.c_uint32()
    rc = getpeereid(sock.fileno(), ctypes.byref(uid), ctypes.byref(gid))
    if rc != 0:
        return None
    return int(uid.value)


__all__ = ["peer_uid_from_socket"]
