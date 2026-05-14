"""Tier 2: LandlockBackend Protocol conformance + availability invariants (FP-0017 Component B)."""
from __future__ import annotations

import sys

import pytest

from reyn.sandbox import SandboxBackend, SandboxPolicy
from reyn.sandbox.backends.landlock import LandlockBackend

# ---------------------------------------------------------------------------
# Platform-independent tests (run on every platform)
# ---------------------------------------------------------------------------


def test_landlock_name_attribute() -> None:
    """Tier 2: name attribute is the literal string 'landlock'."""
    backend = LandlockBackend()
    assert backend.name == "landlock"


def test_landlock_conforms_to_sandbox_backend_protocol() -> None:
    """Tier 2: LandlockBackend satisfies the SandboxBackend runtime-checkable Protocol."""
    backend = LandlockBackend()
    assert isinstance(backend, SandboxBackend)


def test_landlock_unavailable_on_non_linux(monkeypatch: pytest.MonkeyPatch) -> None:
    """Tier 2: available() returns False when platform.system() != 'Linux'."""
    import platform

    monkeypatch.setattr(platform, "system", lambda: "Darwin")
    backend = LandlockBackend()
    assert backend.available() is False


def test_landlock_unavailable_when_landlock_pkg_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tier 2: available() returns False and _import_error is set when landlock is absent."""
    import platform

    # Simulate Linux so we get past the OS check.
    monkeypatch.setattr(platform, "system", lambda: "Linux")

    # Remove landlock from sys.modules so importlib.import_module tries a fresh import.
    monkeypatch.setitem(sys.modules, "landlock", None)  # type: ignore[call-overload]

    backend = LandlockBackend()
    result = backend.available()

    assert result is False
    assert backend._import_error is not None
    assert len(backend._import_error) > 0


def test_landlock_abi_probe_caches(monkeypatch: pytest.MonkeyPatch) -> None:
    """Tier 2: available() only attempts the landlock import once; result is cached."""
    import platform

    monkeypatch.setattr(platform, "system", lambda: "Linux")

    call_count = 0

    # We'll intercept importlib.import_module inside the landlock module to count calls.
    import importlib as _importlib

    original_import = _importlib.import_module

    def counting_import(name: str, *args: object, **kwargs: object) -> object:
        nonlocal call_count
        if name == "landlock":
            call_count += 1
            raise ImportError("landlock not installed (test stub)")
        return original_import(name, *args, **kwargs)  # type: ignore[arg-type]

    # Patch importlib.import_module in the landlock backend module's namespace.
    import reyn.sandbox.backends.landlock as _ll_mod

    monkeypatch.setattr(_ll_mod, "__builtins__", _ll_mod.__builtins__)  # no-op ensure attr
    monkeypatch.setattr(_importlib, "import_module", counting_import)

    backend = LandlockBackend()

    # Call available() three times.
    r1 = backend.available()
    r2 = backend.available()
    r3 = backend.available()

    assert r1 is False
    assert r2 is False
    assert r3 is False
    # The import should only have been attempted once (cached after first call).
    assert call_count == 1


@pytest.mark.asyncio
async def test_landlock_run_raises_when_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tier 2: run() raises RuntimeError mentioning 'not available' on non-Linux."""
    import platform

    monkeypatch.setattr(platform, "system", lambda: "Darwin")
    backend = LandlockBackend()

    with pytest.raises(RuntimeError, match="not available"):
        await backend.run(["echo", "hi"], SandboxPolicy())


# ---------------------------------------------------------------------------
# Linux-only live tests
# ---------------------------------------------------------------------------


@pytest.mark.skipif(sys.platform != "linux", reason="LandlockBackend is Linux-only")
@pytest.mark.asyncio
async def test_landlock_runs_echo_with_read_paths() -> None:
    """Tier 2: on Linux with landlock installed, a basic echo succeeds (returncode 0)."""
    try:
        import landlock  # noqa: F401
    except ImportError:
        pytest.skip("landlock package not installed")

    backend = LandlockBackend()
    if not backend.available():
        pytest.skip("LandlockBackend not available on this kernel")

    policy = SandboxPolicy(
        read_paths=["/bin", "/usr/lib", "/lib"],
        write_paths=[],
        network=False,
        env_passthrough=["PATH"],
        timeout_seconds=10,
    )
    result = await backend.run(["/bin/echo", "hi"], policy)
    assert result.returncode == 0
    assert result.stdout == b"hi\n"


@pytest.mark.skipif(sys.platform != "linux", reason="LandlockBackend is Linux-only")
@pytest.mark.asyncio
async def test_landlock_blocks_writes_outside_policy() -> None:
    """Tier 2: on Linux with landlock, writing outside write_paths yields non-zero returncode."""
    try:
        import landlock  # noqa: F401
    except ImportError:
        pytest.skip("landlock package not installed")

    backend = LandlockBackend()
    if not backend.available():
        pytest.skip("LandlockBackend not available on this kernel")

    import os
    import tempfile

    # Create a temp dir that is NOT in write_paths.
    with tempfile.TemporaryDirectory() as restricted_dir:
        target = os.path.join(restricted_dir, "should_fail.txt")
        policy = SandboxPolicy(
            read_paths=["/bin", "/usr/lib", "/lib"],
            write_paths=[],  # no write paths — kernel should deny
            network=False,
            env_passthrough=["PATH"],
            timeout_seconds=10,
        )
        # Attempt to write a file outside allowed write_paths.
        result = await backend.run(
            ["/bin/sh", "-c", f"echo test > {target}"],
            policy,
        )
        # The write should be blocked by Landlock (EACCES/EPERM → non-zero exit).
        # TODO(fp-0017-b): Linux validation needed — confirm the exact returncode
        # and that the kernel actually denies the write with these access rights.
        assert result.returncode != 0
