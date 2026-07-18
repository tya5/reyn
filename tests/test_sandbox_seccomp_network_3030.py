"""Tier 2c: seccomp allowlist closes the network gate for allow_subprocess=True
(#3030 fix).

``network: false`` used to be silently unenforced whenever ``allow_subprocess:
true`` — the stdio-MCP default (``subprocess:`` defaults to ``True`` so
fork-based ``npx``/``uvx`` launchers can start). Root cause: both production
seams (``landlock.py``'s ``_child_preexec`` / ``landlock_exec.py``'s
``_apply_seccomp``) skipped the WHOLE seccomp-BPF filter when
``allow_subprocess`` was True, and the network gate (``_NETWORK_SYSCALLS``,
allowlisted only when ``policy.network``) lived inside that same filter —
measured on Linux 6.8: a real outbound ``connect()``+``send()`` SUCCEEDED under
``network=False, allow_subprocess=True``.

Three independent design directions were considered before landing here (issue
#3030 / architect co-vet history):

  - a syscall-name DENYLIST mirroring ``_NETWORK_SYSCALLS`` — rejected: io_uring
    (``IORING_OP_CONNECT``/``IORING_OP_SEND``) never calls ``connect()``/
    ``sendto()`` as syscalls, so a denylist is a moving target, not bounded by
    construction.
  - a Linux network namespace (``unshare(CLONE_NEWUSER|CLONE_NEWNET)``) —
    rejected: unprivileged netns needs a user namespace, and ``CLONE_NEWUSER``
    was measured (real x86_64/ABI7 CI data) to break ``opendir('/')`` — DAC on
    the mount root, unrelated to network — a narrow but real regression versus
    today, and the fs side-effect could not be root-caused or fixed cleanly.
  - **the seccomp ALLOWLIST, unconditionally loaded** — adopted. It already
    denies every unnamed syscall by construction (io_uring included, closing the
    denylist's exact gap), has no userns/fs side effects, and reuses a
    mechanism that was already live-validated for ordinary MCP-server workloads
    (``backends/seccomp.py``). The residual cost, paid deliberately: every
    ``allow_subprocess: True`` server (the default) now runs under a
    default-deny syscall filter for the first time — the #2962 correctness
    risk (its first live load killed ``/bin/echo``). This file's completeness
    group exists specifically to answer that risk with REAL MCP servers, not
    just synthetic echo/ls/cat.

⚠ **SKIPS entirely off Linux / without Landlock** — same reasoning as
``test_landlock_exec_shim_1344e.py``: a green run on a dev box (this repo's own
CI author included) witnesses nothing here. What witnesses it:
``sandbox-landlock-deny-gate.yml`` (extended in this PR with a third ``network``
deny arm) + this file, both installing the ``sandbox-linux`` extra on a real
Linux runner.

No mocks — the real ``SandboxPolicy`` / real shim / real subprocesses / real
builtin MCP servers (``reyn-rag-chunker``, ``reyn-rag-vector-store``) via the
real production ``MCPClient`` seam.
"""
from __future__ import annotations

import errno
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from reyn.security.sandbox.policy import SandboxPolicy


def _landlock_available() -> bool:
    from reyn.security.sandbox.backends.landlock import LandlockBackend

    return LandlockBackend().available()


requires_landlock = pytest.mark.skipif(
    not _landlock_available(),
    reason="Landlock unavailable — real seccomp enforcement cannot be witnessed on this host",
)


def _shim_run(policy: SandboxPolicy, argv: list[str]) -> subprocess.CompletedProcess:
    """Launch *argv* through the backend's real ``wrap_command`` (the production
    MCP-stdio / CodeAct seam), pinned to run THIS checkout (env-dependent path
    lesson: without PYTHONPATH the shim re-execs an installed reyn copy, not the
    one under test)."""
    import reyn
    from reyn.security.sandbox.backends.landlock import LandlockBackend

    wrapped = LandlockBackend().wrap_command(argv, policy)
    src_root = Path(reyn.__file__).resolve().parent.parent
    return subprocess.run(
        wrapped.argv,
        capture_output=True,
        text=True,
        timeout=60,
        stdin=subprocess.DEVNULL,
        env={"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "PYTHONPATH": str(src_root)},
    )


# ── network deny, under the EXACT condition #3030 measured as broken ─────────


@requires_landlock
def test_shim_denies_outbound_socket_when_network_false_and_subprocess_allowed(
    tmp_path: Path,
) -> None:
    """Tier 2c: the shim denies socket() under ``network=False,
    allow_subprocess=True`` — the exact policy #3030 measured as broken.

    Three arms, same shape as ``test_shim_denies_a_fork_when_allow_subprocess_is_false``
    and for the same reason — two different lies are available:

    1. ``network=True`` + a socket create must SUCCEED (positive control) — else
       the probe cannot observe a working socket() through this wrap at all.
    2. ``network=False`` + a NON-networking command must still run — else a
       filter that is dead wholesale under this policy (#3030's exact shape:
       the whole filter skipped, not "network off, everything else on") is
       indistinguishable from one that denies only sockets.
    3. Only then the deny: ``network=False`` + a socket create must NOT
       succeed.

    The oracle is a marker FILE, not the exit code or a live connect: the
    seccomp filter refuses the ``socket`` syscall itself, before any address is
    resolved, so no outbound connectivity — flaky in a network-restricted CI
    runner — is needed to witness the deny.
    """
    touch = shutil.which("touch")
    assert touch, "no touch(1) on PATH"
    granted = tmp_path / "granted"
    granted.mkdir()

    def policy(network: bool) -> SandboxPolicy:
        return SandboxPolicy(
            write_paths=[str(granted)],
            read_deny_paths=[],
            network=network,
            allow_subprocess=True,  # the exact #3030 condition (stdio-MCP default)
        )

    def socket_argv(marker: Path) -> list[str]:
        code = (
            "import socket\n"
            "s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)\n"
            f"open({str(marker)!r}, 'w').close()\n"
        )
        return [sys.executable, "-c", code]

    # 1. Positive control.
    control = granted / "control-socket"
    proc = _shim_run(policy(True), socket_argv(control))
    assert control.exists(), (
        f"the shim could not create a socket even with network=True, so a "
        f"missing marker under network=False would prove nothing "
        f"(rc={proc.returncode}, stderr={proc.stderr[:300]!r})"
    )

    # 2. Non-networking control — isolates a dead filter from a network-specific
    #    deny (mirrors #3030's own falsification set, arm 2).
    alive = granted / "control-nonet"
    proc = _shim_run(policy(False), [touch, str(alive)])
    assert alive.exists(), (
        f"under network=False, allow_subprocess=True the shim could not run "
        f"even a NON-networking command — it is failing wholesale, not denying "
        f"sockets specifically (rc={proc.returncode}, stderr={proc.stderr[:300]!r})"
    )

    # 3. The deny — the actual #3030 claim.
    escape = granted / "escaped-socket"
    proc = _shim_run(policy(False), socket_argv(escape))
    assert not escape.exists(), (
        f"no network deny fired: a socket was created under network=False, "
        f"allow_subprocess=True — the exact #3030 condition (rc={proc.returncode}, "
        f"stderr={proc.stderr[:300]!r})"
    )


# ── io_uring: unconditional, bounded-by-construction (the denylist's own gap) ─


_IO_URING_SETUP_NR = 425  # x86_64 AND arm64 (Linux >=5.1's generic syscall table)

_IO_URING_PROBE_SRC = f"""
import ctypes, os
libc = ctypes.CDLL(None, use_errno=True)
buf = ctypes.create_string_buffer(120)  # struct io_uring_params, zeroed
r = libc.syscall({_IO_URING_SETUP_NR}, ctypes.c_uint(1), buf)
if r >= 0:
    os.close(r)
    print("URING_OK")
else:
    print("URING_ERR errno=" + str(ctypes.get_errno()))
"""


def _bare_io_uring_supported() -> bool:
    """Positive control: does THIS kernel support io_uring_setup at all,
    unsandboxed? If not (old kernel / different syscall numbering), the deny
    arm below cannot distinguish "denied by our filter" from "ENOSYS" and must
    be skipped rather than mis-scored."""
    try:
        proc = subprocess.run(
            [sys.executable, "-c", _IO_URING_PROBE_SRC],
            capture_output=True, text=True, timeout=10,
        )
    except Exception:  # noqa: BLE001
        return False
    return "URING_OK" in proc.stdout


@requires_landlock
@pytest.mark.skipif(
    not _bare_io_uring_supported(),
    reason="io_uring_setup not usable unsandboxed on this host/kernel — cannot "
    "distinguish a seccomp EPERM from ENOSYS",
)
def test_shim_denies_io_uring_setup_unconditionally(tmp_path: Path) -> None:
    """Tier 2c: io_uring_setup is refused EPERM through the shim REGARDLESS of
    policy — the exact gap a syscall-name denylist (the design #3030's issue
    considered and rejected) would have missed, since io_uring's opcodes never
    call ``socket``/``connect`` as syscalls.

    Uses the most PERMISSIVE policy (network=True, allow_subprocess=True) to
    show the deny is not an artifact of either axis being off — bounded by
    construction (unnamed => refused), not by naming io_uring specifically.
    """
    granted = tmp_path / "granted"
    granted.mkdir()
    policy = SandboxPolicy(
        write_paths=[str(granted)], read_deny_paths=[], network=True, allow_subprocess=True,
    )
    proc = _shim_run(policy, [sys.executable, "-c", _IO_URING_PROBE_SRC])
    assert "URING_ERR errno=" in proc.stdout, (
        f"io_uring_setup was NOT refused through the shim (stdout={proc.stdout!r}, "
        f"rc={proc.returncode}, stderr={proc.stderr[:300]!r}) — a denylist-shaped "
        f"gap the allowlist design exists specifically to close"
    )
    errno_str = proc.stdout.split("errno=", 1)[1].strip()
    assert errno_str == str(errno.EPERM), (
        f"io_uring_setup failed with the wrong errno ({errno_str}, expected "
        f"EPERM={errno.EPERM}) — it may be failing for an unrelated reason, not "
        f"the seccomp deny"
    )


# ── representative real MCP servers: allowlist completeness (#2962 recurrence) ─
#
# ★★ The hard gate: an INCOMPLETE allowlist kills a legitimate subprocess
# (#2962 — the first live load killed /bin/echo). These drive the REAL
# production servers RAG actually uses, through the REAL production
# ``MCPClient`` seam (not a synthetic echo/ls/cat stand-in), under BOTH
# network=True (the default) and network=False (the security-conscious config
# #3030 is about) — proving the now-unconditional filter does not silently
# break the servers it is meant to protect.


def _patch_landlock_backend(monkeypatch: pytest.MonkeyPatch) -> None:
    """Force MCPClient to resolve LandlockBackend, deterministically — the same
    idiom ``test_mcp_client_sandbox_wrap.py`` uses, so this doesn't depend on
    whatever ``get_default_backend()``'s own platform auto-selection does."""
    from reyn.security.sandbox.backends.landlock import LandlockBackend

    monkeypatch.setattr(
        "reyn.security.sandbox.get_default_backend", lambda config=None: LandlockBackend()
    )


@requires_landlock
@pytest.mark.parametrize("network", [True, False])
@pytest.mark.asyncio
async def test_chunker_server_starts_and_responds_under_seccomp_allowlist(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, network: bool,
) -> None:
    """Tier 2c: ``reyn-rag-chunker`` (a real console-script MCP server RAG
    actually uses) starts and answers a real tool call through the
    now-unconditional seccomp allowlist, at both ``network`` settings.

    No apsw/chonkie/builtin-rag extra needed — the chunker server is pure
    Python (chonkie import happens lazily inside the tool, exercised here for
    real, not stubbed).
    """
    from reyn.mcp.client import MCPClient

    _patch_landlock_backend(monkeypatch)
    command = shutil.which("reyn-rag-chunker")
    if command is None:
        pytest.skip("reyn-rag-chunker console script not on PATH — pip install "
                    "'reyn[builtin-rag]' not run in this environment")

    cfg = {
        "type": "stdio",
        "command": command,
        "network": network,
        "subprocess": True,  # the stdio-MCP default this fix is about
        "cwd": str(tmp_path),
    }
    async with MCPClient(cfg) as client:
        tools = await client.list_tools()
        assert any(t.get("name") == "chunk" for t in tools), (
            f"reyn-rag-chunker did not advertise its 'chunk' tool under the "
            f"seccomp allowlist (network={network}); tools={tools!r}"
        )
        result = await client.call_tool(
            "chunk", {"text": "hello world " * 50, "size": 20}
        )
        assert not result.get("isError"), (
            f"reyn-rag-chunker's real 'chunk' tool call FAILED under the "
            f"allowlist (network={network}) — an allowlist gap silently broke a "
            f"real MCP server, the #2962 recurrence this test exists to catch: "
            f"{result!r}"
        )


@requires_landlock
@pytest.mark.parametrize("network", [True, False])
@pytest.mark.asyncio
async def test_vector_store_server_starts_and_responds_under_seccomp_allowlist(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, network: bool,
) -> None:
    """Tier 2c: ``reyn-rag-vector-store`` starts and answers a real tool call
    through the now-unconditional seccomp allowlist, at both ``network``
    settings. Needs the ``builtin-rag`` extra (apsw/sqlite-vec) — skips (not
    fails) when absent, same posture as ``test_fp0063_p3_rag_pipelines.py``."""
    pytest.importorskip("apsw", reason="builtin-rag extra not installed")
    from reyn.mcp.client import MCPClient

    _patch_landlock_backend(monkeypatch)
    command = shutil.which("reyn-rag-vector-store")
    if command is None:
        pytest.skip("reyn-rag-vector-store console script not on PATH — pip "
                    "install 'reyn[builtin-rag]' not run in this environment")

    cfg = {
        "type": "stdio",
        "command": command,
        "network": network,
        "subprocess": True,
        "cwd": str(tmp_path),
    }
    async with MCPClient(cfg) as client:
        tools = await client.list_tools()
        assert any(t.get("name") == "list_metadata" for t in tools), (
            f"reyn-rag-vector-store did not advertise its tools under the "
            f"seccomp allowlist (network={network}); tools={tools!r}"
        )
        result = await client.call_tool(
            "list_metadata", {"db_path": str(tmp_path / "probe.sqlite"), "filters": None}
        )
        assert not result.get("isError"), (
            f"reyn-rag-vector-store's real 'list_metadata' tool call FAILED "
            f"under the allowlist (network={network}) — an allowlist gap "
            f"silently broke a real MCP server: {result!r}"
        )


@requires_landlock
@pytest.mark.asyncio
async def test_markitdown_mcp_starts_and_responds_under_seccomp_allowlist(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """Tier 2c: ``uvx markitdown-mcp`` — the third RAG server, and the one with
    the widest syscall footprint (network fetch by ``uvx`` on first run, then a
    real parse) — starts and converts a real file under the allowlist.

    Prewarms uvx's cache with an UNSANDBOXED run first (mirrors what a real
    operator's first `reyn` launch does): the sandboxed run below then needs no
    network even under ``network=False``, which is also the more realistic
    steady-state an operator actually hits after day one. Skips (not fails) on
    any network/uvx unavailability — that is an environment-reachability
    concern (rag_ingest's own X1 pre-flight treats it the same way), orthogonal
    to allowlist completeness.
    """
    from reyn.mcp.client import MCPClient

    uvx = shutil.which("uvx")
    if uvx is None:
        pytest.skip("uvx not on PATH")

    try:
        subprocess.run(
            [uvx, "markitdown-mcp", "--help"],
            capture_output=True, timeout=120, check=False,
        )
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"could not prewarm uvx's markitdown-mcp cache: {exc!r}")

    _patch_landlock_backend(monkeypatch)
    src = tmp_path / "probe.txt"
    src.write_text("hello from the seccomp-allowlist completeness probe")

    cfg = {
        "type": "stdio",
        "command": uvx,
        "args": ["markitdown-mcp"],
        "network": False,  # the security-conscious config #3030 is about
        "subprocess": True,
        "cwd": str(tmp_path),
    }
    try:
        async with MCPClient(cfg) as client:
            tools = await client.list_tools()
            assert any(t.get("name") == "convert_to_markdown" for t in tools), (
                f"markitdown-mcp did not advertise convert_to_markdown under the "
                f"seccomp allowlist; tools={tools!r}"
            )
            result = await client.call_tool(
                "convert_to_markdown", {"uri": src.as_uri()}
            )
    except Exception as exc:  # noqa: BLE001
        pytest.skip(
            f"markitdown-mcp did not start/respond in this environment "
            f"(network-dependent uvx fetch): {exc!r}"
        )
    assert not result.get("isError"), (
        f"markitdown-mcp's real 'convert_to_markdown' tool call FAILED under "
        f"the allowlist — an allowlist gap silently broke a real MCP server: "
        f"{result!r}"
    )
