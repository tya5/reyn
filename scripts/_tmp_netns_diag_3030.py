"""TEMPORARY diagnostic (#3030) — root-cause the x86_64/ABI7 opendir('/') EACCES
under netns+Landlock. Deleted before this PR merges.

Runs each step of the sandbox stack in isolation, in a fresh forked child, and
reports uid/gid + opendir('/') outcome after each, so we can see EXACTLY which
step turns a working broad-read into EACCES on the CI runner (which we cannot
reproduce on aarch64/ABI4).
"""
from __future__ import annotations

import ctypes
import os
import sys


def _probe(label: str) -> None:
    import tempfile
    parts = []
    for d in ("/", "/usr", "/lib", "/bin", "/etc", "/tmp", "/usr/lib", "/home"):
        try:
            os.listdir(d)
            parts.append(f"ls({d})=OK")
        except OSError as e:
            parts.append(f"ls({d})=FAIL({e.errno})")
    try:
        td = tempfile.mkdtemp()
        with open(os.path.join(td, "f"), "w") as fh:
            fh.write("x")
        with open(os.path.join(td, "f")) as fh:
            fh.read()
        os.listdir(td)
        parts.append("tmpdir-rw=OK")
    except OSError as e:
        parts.append(f"tmpdir-rw=FAIL({e.errno})")
    print(f"[{label}] uid={os.getuid()} gid={os.getgid()} " + " ".join(parts), flush=True)


def _in_child(fn) -> None:
    pid = os.fork()
    if pid == 0:
        try:
            fn()
            os._exit(0)
        except BaseException as e:  # noqa: BLE001
            print(f"  child raised: {e!r}", flush=True)
            os._exit(1)
    os.waitpid(pid, 0)


def _step_userns_only() -> None:
    libc = ctypes.CDLL(None, use_errno=True)
    _probe("before")
    rc = libc.unshare(0x10000000 | 0x40000000)  # NEWUSER|NEWNET
    print(f"  unshare(NEWUSER|NEWNET) rc={rc} errno={ctypes.get_errno() if rc else '-'}", flush=True)
    _probe("after-userns-nomap")
    # try the identity map
    try:
        with open("/proc/self/setgroups", "w") as fh:
            fh.write("deny")
        print("  setgroups=deny OK", flush=True)
    except OSError as e:
        print(f"  setgroups write FAIL: {e!r}", flush=True)
    try:
        ru = os.getuid()
        with open("/proc/self/uid_map", "w") as fh:
            fh.write(f"{ru} {ru} 1")
        print(f"  uid_map '{ru} {ru} 1' OK", flush=True)
    except OSError as e:
        print(f"  uid_map write FAIL: {e!r}", flush=True)
    try:
        rg = os.getgid()
        with open("/proc/self/gid_map", "w") as fh:
            fh.write(f"{rg} {rg} 1")
        print(f"  gid_map '{rg} {rg} 1' OK", flush=True)
    except OSError as e:
        print(f"  gid_map write FAIL: {e!r}", flush=True)
    _probe("after-identity-map")


def _step_landlock_only() -> None:
    """Landlock ruleset applied WITHOUT netns — baseline (should be OK, this is
    what main does)."""
    from reyn.security.sandbox.backends.landlock import build_ruleset
    from reyn.security.sandbox.policy import SandboxPolicy
    import tempfile

    wd = tempfile.mkdtemp()
    pol = SandboxPolicy(write_paths=[wd])
    from reyn.security.sandbox.backends.landlock import LandlockBackend
    rs = build_ruleset(pol, LandlockBackend().abi_version or 0)
    _probe("before-landlock")
    rs.apply()
    _probe("after-landlock-only")


def _step_userns_then_landlock() -> None:
    """netns THEN Landlock — the current _child_preexec order."""
    from reyn.security.sandbox.backends.landlock import build_ruleset, LandlockBackend
    from reyn.security.sandbox.policy import SandboxPolicy
    import tempfile

    wd = tempfile.mkdtemp()
    pol = SandboxPolicy(write_paths=[wd])
    rs = build_ruleset(pol, LandlockBackend().abi_version or 0)
    libc = ctypes.CDLL(None, use_errno=True)
    rc = libc.unshare(0x10000000 | 0x40000000)
    print(f"  unshare rc={rc}", flush=True)
    _probe("after-userns")
    rs.apply()
    _probe("after-userns-then-landlock")


def _step_landlock_then_userns() -> None:
    """Landlock THEN netns — the reordering hypothesis."""
    from reyn.security.sandbox.backends.landlock import build_ruleset, LandlockBackend
    from reyn.security.sandbox.policy import SandboxPolicy
    import tempfile

    wd = tempfile.mkdtemp()
    pol = SandboxPolicy(write_paths=[wd])
    rs = build_ruleset(pol, LandlockBackend().abi_version or 0)
    rs.apply()
    _probe("after-landlock")
    libc = ctypes.CDLL(None, use_errno=True)
    rc = libc.unshare(0x10000000 | 0x40000000)
    print(f"  unshare rc={rc} errno={ctypes.get_errno() if rc else '-'}", flush=True)
    _probe("after-landlock-then-userns")


def _step_parent_writes_child_map() -> None:
    """The standard container-runtime approach: the PARENT (in the init userns,
    with CAP_SETUID over the child's new userns) writes the child's uid_map,
    which is NOT subject to the single-line self-map restriction that blocks the
    child writing its own. Tests whether AppArmor's unprivileged-userns
    restriction also blocks THIS (the child self-map is already known blocked)."""
    r_uid, r_gid = os.getuid(), os.getgid()
    c2p_r, c2p_w = os.pipe()  # child -> parent: "I've unshared"
    p2c_r, p2c_w = os.pipe()  # parent -> child: "map written, continue"
    pid = os.fork()
    if pid == 0:
        os.close(c2p_r)
        os.close(p2c_w)
        libc = ctypes.CDLL(None, use_errno=True)
        rc = libc.unshare(0x10000000 | 0x40000000)
        os.write(c2p_w, b"1" if rc == 0 else b"0")
        os.close(c2p_w)
        os.read(p2c_r, 1)  # wait for parent to write the map
        _probe("child-after-parent-map")
        os._exit(0)
    os.close(c2p_w)
    os.close(p2c_r)
    ok = os.read(c2p_r, 1)
    print(f"  child unshared: {ok!r}", flush=True)
    # Parent writes the CHILD's maps.
    for name, val in (
        ("setgroups", "deny"),
        ("uid_map", f"{r_uid} {r_uid} 1"),
        ("gid_map", f"{r_gid} {r_gid} 1"),
    ):
        try:
            with open(f"/proc/{pid}/{name}", "w") as fh:
                fh.write(val)
            print(f"  parent wrote /proc/{pid}/{name} = {val!r} OK", flush=True)
        except OSError as e:
            print(f"  parent write /proc/{pid}/{name} FAIL: {e!r}", flush=True)
    os.write(p2c_w, b"1")
    os.close(p2c_w)
    os.waitpid(pid, 0)


def main() -> int:
    from reyn.security.sandbox.backends.landlock import LandlockBackend
    b = LandlockBackend()
    print(f"Landlock available={b.available()} ABI={b.abi_version}", flush=True)
    import platform
    print(f"machine={platform.machine()} kernel={platform.release()}", flush=True)
    print("\n=== userns only (no Landlock) ===", flush=True)
    _in_child(_step_userns_only)
    print("\n=== Landlock only (no userns) — baseline ===", flush=True)
    _in_child(_step_landlock_only)
    print("\n=== userns THEN Landlock (current order) ===", flush=True)
    _in_child(_step_userns_then_landlock)
    print("\n=== Landlock THEN userns (reorder hypothesis) ===", flush=True)
    _in_child(_step_landlock_then_userns)
    print("\n=== parent-writes-child-uid_map handshake ===", flush=True)
    _step_parent_writes_child_map()
    return 0


if __name__ == "__main__":
    sys.exit(main())
