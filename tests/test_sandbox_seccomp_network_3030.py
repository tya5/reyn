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
builtin ``rag`` plugin MCP server scripts (``chunker_server.py``,
``vector_store_server.py``, launched directly — ADR 0064 P5 retired the
``reyn-rag-chunker``/``reyn-rag-vector-store`` console scripts) via the real
production ``MCPClient`` seam.
"""
from __future__ import annotations

import errno
import os
import shutil
import socket
import subprocess
import sys
from pathlib import Path

import pytest

from reyn.security.sandbox.policy import SandboxPolicy


def _rag_plugin_script(name: str) -> Path:
    """Path to a builtin ``rag`` plugin MCP server script
    (``src/reyn/builtin/plugins/rag/scripts/<name>``), resolved package-
    relative so this works identically in a dev checkout or an installed
    wheel (ADR 0064 P5 — mirrors ``plugin_install._builtin_plugin_dir``)."""
    import reyn.builtin as _builtin_pkg

    return Path(_builtin_pkg.__file__).resolve().parent / "plugins" / "rag" / "scripts" / name


def _landlock_available() -> bool:
    from reyn.security.sandbox.backends.landlock import LandlockBackend

    return LandlockBackend().available()


requires_landlock = pytest.mark.skipif(
    not _landlock_available(),
    reason="Landlock unavailable — real seccomp enforcement cannot be witnessed on this host",
)


def _shim_run(
    src_root: str, policy: SandboxPolicy, argv: list[str]
) -> subprocess.CompletedProcess:
    """Launch *argv* through the backend's real ``wrap_command`` (the production
    MCP-stdio / CodeAct seam), pinned to run THIS checkout via *src_root* (the
    ``out_of_process_reyn`` fixture value — env-dependent path lesson: without
    PYTHONPATH the shim re-execs an installed reyn copy, not the one under
    test). Custom ``PATH`` preserved — the sandbox test needs it."""
    from reyn.security.sandbox.backends.landlock import LandlockBackend

    wrapped = LandlockBackend().wrap_command(argv, policy)
    return subprocess.run(
        wrapped.argv,
        capture_output=True,
        text=True,
        timeout=60,
        stdin=subprocess.DEVNULL,
        env={"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "PYTHONPATH": src_root},
    )


# ── network deny, under the EXACT condition #3030 measured as broken ─────────


@requires_landlock
def test_shim_denies_outbound_connect_when_network_false_and_subprocess_allowed(
    tmp_path: Path, out_of_process_reyn: str,
) -> None:
    """Tier 2c: the shim denies connect() under ``network=False,
    allow_subprocess=True`` — the exact policy #3030 measured as broken.

    Updated for #3060: ``socket``/``bind`` moved to the always-allowed set (a
    surgical fix for a benign, loopback-only urllib3 IPv6-support probe that
    used to be refused as collateral damage), so a ``socket()``-create is no
    longer evidence of anything — see
    ``test_shim_allows_socket_and_bind_when_network_false`` below for that
    axis. The witness for the actual egress claim moves to ``connect()``,
    which stays gated on ``policy.network``.

    Three arms, same shape as ``test_shim_denies_a_fork_when_allow_subprocess_is_false``
    and for the same reason — two different lies are available:

    1. ``network=True`` + a connect() to a loopback listener must SUCCEED
       (positive control) — else the probe cannot observe a working
       connect() through this wrap at all.
    2. ``network=False`` + a NON-networking command must still run — else a
       filter that is dead wholesale under this policy (#3030's exact shape:
       the whole filter skipped, not "network off, everything else on") is
       indistinguishable from one that denies only sockets.
    3. Only then the deny: ``network=False`` + connect() must NOT succeed.

    The oracle is a marker FILE, not the exit code: the listener is opened in
    THIS (unsandboxed) process, so the sandboxed script performs exactly one
    network syscall (``connect()``) with no outbound connectivity risk on a
    network-restricted CI runner.
    """
    touch = shutil.which("touch")
    assert touch, "no touch(1) on PATH"
    granted = tmp_path / "granted"
    granted.mkdir()

    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen(5)
    loopback_port = listener.getsockname()[1]
    try:
        def policy(network: bool) -> SandboxPolicy:
            return SandboxPolicy(
                write_paths=[str(granted)],
                read_deny_paths=[],
                network=network,
                allow_subprocess=True,  # the exact #3030 condition (stdio-MCP default)
            )

        def connect_argv(marker: Path) -> list[str]:
            code = (
                "import socket\n"
                "c = socket.socket(socket.AF_INET, socket.SOCK_STREAM)\n"
                f"c.connect(('127.0.0.1', {loopback_port}))\n"
                f"open({str(marker)!r}, 'w').close()\n"
            )
            return [sys.executable, "-c", code]

        # 1. Positive control.
        control = granted / "control-connect"
        proc = _shim_run(out_of_process_reyn, policy(True), connect_argv(control))
        assert control.exists(), (
            f"the shim could not connect() even with network=True, so a "
            f"missing marker under network=False would prove nothing "
            f"(rc={proc.returncode}, stderr={proc.stderr[:300]!r})"
        )

        # 2. Non-networking control — isolates a dead filter from a network-specific
        #    deny (mirrors #3030's own falsification set, arm 2).
        alive = granted / "control-nonet"
        proc = _shim_run(out_of_process_reyn, policy(False), [touch, str(alive)])
        assert alive.exists(), (
            f"under network=False, allow_subprocess=True the shim could not run "
            f"even a NON-networking command — it is failing wholesale, not denying "
            f"connect() specifically (rc={proc.returncode}, stderr={proc.stderr[:300]!r})"
        )

        # 3. The deny — the actual #3030 claim.
        escape = granted / "escaped-connect"
        proc = _shim_run(out_of_process_reyn, policy(False), connect_argv(escape))
        assert not escape.exists(), (
            f"no network deny fired: connect() succeeded under network=False, "
            f"allow_subprocess=True — the exact #3030 condition (rc={proc.returncode}, "
            f"stderr={proc.stderr[:300]!r})"
        )
    finally:
        listener.close()


@requires_landlock
def test_shim_allows_socket_and_bind_when_network_false(tmp_path: Path, out_of_process_reyn: str) -> None:
    """Tier 2c: #3060 — socket() and a LOOPBACK bind() both succeed through the
    shim even under ``network=False, allow_subprocess=True``, the exact
    condition under which urllib3's import-time IPv6-support probe
    (``socket()`` then ``bind(("::1", 0))``, never a ``connect()``) used to be
    refused as collateral damage of the network gate.

    This is the "(a) chunker builtin server starts under network:false"
    regression witness at the syscall level: the representative-real-MCP-server
    probes below (``test_chunker_server_starts_and_responds_under_seccomp_allowlist``
    etc.) exercise the same guarantee end-to-end through a real FastMCP server.
    """
    granted = tmp_path / "granted"
    granted.mkdir()
    policy = SandboxPolicy(
        write_paths=[str(granted)],
        read_deny_paths=[],
        network=False,
        allow_subprocess=True,
    )
    marker = granted / "bind-ok"
    code = (
        "import socket\n"
        "s = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)\n"
        "s.bind(('::1', 0))\n"
        f"open({str(marker)!r}, 'w').close()\n"
    )
    proc = _shim_run(out_of_process_reyn, policy, [sys.executable, "-c", code])
    assert marker.exists(), (
        f"socket()+bind(('::1', 0)) must succeed under network=False — this is "
        f"the exact urllib3 IPv6-probe shape #3060 fixes "
        f"(rc={proc.returncode}, stderr={proc.stderr[:300]!r})"
    )


# ── #3060 case-(b): the async self-pipe. NULL-addr sendto/recvfrom SURVIVE, ────
# ── an ADDRESSED sendto stays DENIED. The two must be witnessed separately. ────


@requires_landlock
def test_shim_allows_null_addr_socketpair_sendto_recvfrom_when_network_false(
    tmp_path: Path, out_of_process_reyn: str,
) -> None:
    """Tier 2c: #3060 case-(b) POSITIVE witness — ``sendto``/``recvfrom`` with a
    NULL address pointer SUCCEED under ``network=False``.

    This is the mechanism every stdio MCP server needs: CPython's asyncio event
    loop wakes itself through a *connected* AF_UNIX socketpair self-pipe, whose
    ``send()``/``recv()`` lower to the ``sendto``/``recvfrom`` SYSCALLS with a
    NULL addr (a connected socket carries no address). Denying them wholesale —
    the pre-#3060-fix state — left the loop unable to pump, so the server
    completed ``run_forever`` but produced 0 bytes and the client's MCP
    ``initialize`` handshake timed out (measured via the client-side
    raw-handshake capture). Allowing the NULL-addr form restores the wakeup.
    """
    granted = tmp_path / "granted"
    granted.mkdir()
    policy = SandboxPolicy(
        write_paths=[str(granted)],
        read_deny_paths=[],
        network=False,
        allow_subprocess=True,
    )
    marker = granted / "socketpair-ok"
    code = (
        "import socket\n"
        # Default AF_UNIX SOCK_STREAM, connected — exactly asyncio's self-pipe.
        "a, b = socket.socketpair()\n"
        # send() -> sendto(fd, buf, len, flags, NULL, 0)  (arg4 == 0)
        "a.send(b'ping')\n"
        # recv() -> recvfrom(fd, buf, len, flags, NULL, NULL)  (arg4 == 0)
        "assert b.recv(4) == b'ping'\n"
        f"open({str(marker)!r}, 'w').close()\n"
    )
    proc = _shim_run(out_of_process_reyn, policy, [sys.executable, "-c", code])
    assert marker.exists(), (
        f"NULL-addr socketpair sendto/recvfrom must SUCCEED under network=False "
        f"(the async event-loop self-pipe #3060 fixes) — rc={proc.returncode}, "
        f"stderr={proc.stderr[:400]!r}"
    )


@requires_landlock
def test_shim_denies_addressed_sendto_when_network_false(
    tmp_path: Path, out_of_process_reyn: str,
) -> None:
    """Tier 2c: #3060 case-(b) NEGATIVE witness (egress-safety — the load-bearing
    one) — an ADDRESSED ``sendto`` (real UDP egress,
    ``sendto(fd, buf, len, flags, &sockaddr_in, addrlen)``) stays EPERM-DENIED
    under ``network=False``.

    Why this must be witnessed SEPARATELY from the socketpair positive above: the
    chunker-boot and socketpair witnesses only ever exercise the NULL-addr form,
    so a mis-implementation that allowed ``sendto`` UNCONDITIONALLY (dropping the
    ``arg4 == 0`` condition) would pass every one of them while silently
    reopening UDP egress — a datagram socket can exfiltrate with a single
    addressed ``sendto``, no ``connect`` required. Only this probe proves the
    NULL-address exception did not become an unconditional hole.

    Marker-file oracle (not a live packet): the seccomp filter refuses the
    ``sendto`` syscall on a non-NULL arg 4 before anything leaves the host, so no
    outbound connectivity — flaky on a network-restricted runner — is needed.
    Strip-falsify: drop the ``arg4 == 0`` condition (allow ``sendto``
    unconditionally) and this test goes RED (the marker is created); restore it
    and it is GREEN.
    """
    granted = tmp_path / "granted"
    granted.mkdir()
    policy = SandboxPolicy(
        write_paths=[str(granted)],
        read_deny_paths=[],
        network=False,
        allow_subprocess=True,
    )
    marker = granted / "addressed-sendto-happened"
    code = (
        "import socket\n"
        "s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)\n"
        # Addressed -> non-NULL arg 4 -> must fall through to default-deny EPERM.
        "s.sendto(b'x', ('93.184.216.34', 53))\n"
        f"open({str(marker)!r}, 'w').close()\n"
    )
    proc = _shim_run(out_of_process_reyn, policy, [sys.executable, "-c", code])
    assert not marker.exists(), (
        f"addressed sendto (real UDP egress) MUST stay denied under "
        f"network=False — the NULL-addr sendto/recvfrom exception must NOT open "
        f"the addressed form (rc={proc.returncode}, stderr={proc.stderr[:400]!r})"
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
def test_shim_denies_io_uring_setup_unconditionally(
    tmp_path: Path, out_of_process_reyn: str,
) -> None:
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
    proc = _shim_run(out_of_process_reyn, policy, [sys.executable, "-c", _IO_URING_PROBE_SRC])
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
# ── why network=True (not parametrized over network=False) ───────────────────
#
# Historical measurement (#3059 co-vet, deny-gate x86_64, PRE-#3060 filter): a
# FastMCP-based stdio server issued a network-family syscall during its init
# handshake, so under `network=False` the seccomp filter denied it and the
# server could not initialize (Connection closed) — chunker/vector-store init
# succeeded at network=True and failed at network=False, a network-flag-
# correlated result attributed at the time to `socket`/`connect`.
#
# #3060 (option A) supersedes that network=False behavior for these two servers:
# `socket`/`bind` are now ALWAYS allowed (`_NETWORK_ALWAYS_ALLOWED` — the benign
# urllib3 import-time IPv6-support probe no longer dies as collateral), and the
# builtin RAG servers are launched with FastMCP telemetry/update-check disabled
# (their `.mcp.json` sets `FASTMCP_SHOW_SERVER_BANNER=false`/
# `FASTMCP_CHECK_FOR_UPDATES=off`), so the phone-home `connect()` is not
# attempted. Per the #3060 architect firm the chunker/vector-store therefore now
# init cleanly under network=False as well.
#
# These completeness probes nonetheless still run at network=True, for two
# reasons that survive #3060: (1) `uvx markitdown-mcp` GENUINELY fetches its
# package over the network, so that server needs network=True regardless;
# running all three uniformly at network=True keeps them comparable and (2)
# isolates the "#2962 recurrence: does the unconditional filter break a server
# that would otherwise work?" allowlist-completeness question from any network
# behavior — the syscall filter stays the ONLY variable vs baseline. The
# network=False EGRESS deny (a `connect()` is refused, `socket()`/`bind()` are
# not) is witnessed precisely, without this init confound, by
# `test_shim_denies_outbound_connect_*` + `test_shim_allows_socket_and_bind_*`
# + the io_uring probe + the deny-gate `network` arm above.


@pytest.mark.asyncio
async def test_chunker_server_starts_and_responds_under_seccomp_allowlist(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """Tier 2c: the builtin ``rag`` plugin's chunker server (a real MCP server
    RAG actually uses) starts and answers a real tool call through the
    now-unconditional seccomp allowlist.

    Runs at ``network=True`` — see the "why network=True" note above. Needs the
    ``builtin-rag`` extra (chonkie import happens lazily inside the tool,
    exercised here for real, not stubbed). Launched directly as
    ``<this interpreter> <plugin script path>`` — ADR 0064 P5 retired the
    ``reyn-rag-chunker`` console script (a real plugin install spawns via a
    materialised per-plugin venv's own interpreter instead); this test's
    job is the seccomp-allowlist completeness property, which needs only
    SOME real chonkie-backed MCP server process, not the install mechanism
    itself (covered by tests/test_plugin_install.py)."""
    pytest.importorskip("chonkie", reason="builtin-rag extra not installed")
    from reyn.mcp.client import MCPClient

    _patch_landlock_backend(monkeypatch)
    script = _rag_plugin_script("chunker_server.py")
    if not script.exists():  # pragma: no cover - packaging regression only
        pytest.skip(f"builtin rag plugin chunker script not found at {script}")

    cfg = {
        "type": "stdio",
        "command": sys.executable,
        "args": [str(script)],
        "network": True,
        "subprocess": True,  # the stdio-MCP default this fix is about
        "cwd": str(tmp_path),
    }
    async with MCPClient(cfg) as client:
        tools = await client.list_tools()
        assert any(t.get("name") == "chunk" for t in tools), (
            f"reyn-rag-chunker did not advertise its 'chunk' tool under the "
            f"seccomp allowlist; tools={tools!r}"
        )
        result = await client.call_tool(
            "chunk", {"text": "hello world " * 50, "size": 20}
        )
        assert not result.get("isError"), (
            f"reyn-rag-chunker's real 'chunk' tool call FAILED under the "
            f"allowlist — an allowlist gap silently broke a real MCP server, "
            f"the #2962 recurrence this test exists to catch: {result!r}"
        )


@requires_landlock
@pytest.mark.asyncio
async def test_chunker_server_reaches_serving_under_network_false(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """Tier 2c: #3060 reachable-for-purpose witness — the real chunker FastMCP
    server INITIALIZES AND SERVES under ``network=False`` (the whole point of
    #3060), not merely "socket/bind are allowed at the syscall level".

    The syscall-level probes (``test_shim_allows_socket_and_bind_*``) prove the
    allow-set exists; this proves the allow-set is SUFFICIENT for the actual
    purpose — a real chonkie-backed FastMCP stdio server, launched with the two
    telemetry/update-check env vars the plugin's ``.mcp.json`` sets
    (``FASTMCP_SHOW_SERVER_BANNER=false`` / ``FASTMCP_CHECK_FOR_UPDATES=off``,
    which suppress FastMCP's banner-time phone-home ``connect()`` to PyPI),
    reaches serving (advertises + answers its ``chunk`` tool) rather than dying
    in ``initialize()``. Mechanism-exists ≠ reachable-for-purpose; this closes
    that gap.

    Contrast the sibling completeness probe above, which runs at
    ``network=True`` to isolate the allowlist-completeness question from network
    behavior (see the "why network=True" note). This one deliberately fixes
    ``network=False`` because THAT is the config #3060 makes work — and pins the
    telemetry env exactly as the shipped ``.mcp.json`` does, so a regression that
    dropped either env var (re-enabling the PyPI ``connect()`` that
    ``network=False`` refuses) would fail here.

    Real chunker + real seccomp shim via the real ``MCPClient`` seam, no fakes;
    Linux-CI-gated (``@requires_landlock``), so it SKIPS on darwin/without
    Landlock exactly like the sibling probes — a green run on a dev box witnesses
    nothing here."""
    pytest.importorskip("chonkie", reason="builtin-rag extra not installed")
    from reyn.mcp.client import MCPClient

    _patch_landlock_backend(monkeypatch)
    script = _rag_plugin_script("chunker_server.py")
    if not script.exists():  # pragma: no cover - packaging regression only
        pytest.skip(f"builtin rag plugin chunker script not found at {script}")

    cfg = {
        "type": "stdio",
        "command": sys.executable,
        "args": [str(script)],
        "network": False,  # the exact config #3060 makes work for the chunker
        "subprocess": True,  # the stdio-MCP default
        "cwd": str(tmp_path),
        # Exactly the vars the shipped .mcp.json sets — suppress FastMCP's
        # banner-time update check (a real httpx.get to pypi.org) so no
        # outbound connect() is attempted that network=False would refuse.
        "env": {
            "FASTMCP_SHOW_SERVER_BANNER": "false",
            "FASTMCP_CHECK_FOR_UPDATES": "off",
        },
    }
    async with MCPClient(cfg) as client:
        # Reaching list_tools() at all means the server survived initialize()
        # under network=False — it did not die on a refused network syscall.
        tools = await client.list_tools()
        assert any(t.get("name") == "chunk" for t in tools), (
            f"reyn-rag-chunker did not reach serving under network=False — it "
            f"advertised no 'chunk' tool, so the server failed to initialize "
            f"under the exact config #3060 is meant to make work; tools={tools!r}"
        )
        # And it can actually do its job (local chonkie processing, no network).
        result = await client.call_tool(
            "chunk", {"text": "hello world " * 50, "size": 20}
        )
        assert not result.get("isError"), (
            f"reyn-rag-chunker reached serving but its real 'chunk' call FAILED "
            f"under network=False — #3060's purpose (the chunker actually works "
            f"with network off) is not met: {result!r}"
        )


@requires_landlock
@pytest.mark.asyncio
async def test_vector_store_server_starts_and_responds_under_seccomp_allowlist(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """Tier 2c: the builtin ``rag`` plugin's vector-store server starts and
    answers a real tool call through the now-unconditional seccomp allowlist.

    Runs at ``network=True`` (see the "why network=True" note above). The tool
    exercised — ``list_metadata`` — is a LOCAL sqlite operation, so it is what
    surfaced the `fsync`/`fdatasync` durability gap (SQLite "disk I/O error"),
    network-independently. Needs the ``builtin-rag`` extra (apsw/sqlite-vec) —
    skips (not fails) when absent, same posture as
    ``test_fp0063_p3_rag_pipelines.py``. See the chunker test above for why
    this launches the plugin script directly rather than via a console
    script (ADR 0064 P5)."""
    pytest.importorskip("apsw", reason="builtin-rag extra not installed")
    from reyn.mcp.client import MCPClient

    _patch_landlock_backend(monkeypatch)
    script = _rag_plugin_script("vector_store_server.py")
    if not script.exists():  # pragma: no cover - packaging regression only
        pytest.skip(f"builtin rag plugin vector-store script not found at {script}")

    cfg = {
        "type": "stdio",
        "command": sys.executable,
        "args": [str(script)],
        "network": True,
        "subprocess": True,
        "cwd": str(tmp_path),
    }
    async with MCPClient(cfg) as client:
        tools = await client.list_tools()
        assert any(t.get("name") == "list_metadata" for t in tools), (
            f"reyn-rag-vector-store did not advertise its tools under the "
            f"seccomp allowlist; tools={tools!r}"
        )
        result = await client.call_tool(
            "list_metadata", {"db_path": str(tmp_path / "probe.sqlite"), "filters": None}
        )
        assert not result.get("isError"), (
            f"reyn-rag-vector-store's real 'list_metadata' tool call FAILED "
            f"under the allowlist — an allowlist gap (e.g. sqlite fsync) silently "
            f"broke a real MCP server: {result!r}"
        )


@requires_landlock
@pytest.mark.asyncio
async def test_markitdown_mcp_starts_and_responds_under_seccomp_allowlist(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """Tier 2c: ``uvx markitdown-mcp`` — the third RAG server, and the one with
    the widest syscall footprint (uv's Tokio runtime + cache lock, then a real
    parse) — starts and converts a real file under the allowlist.

    Runs at ``network=True`` (see the "why network=True" note above the chunker
    test): uv/markitdown make network-family syscalls at startup, so this
    isolates allowlist completeness from the server's own network use. Note the
    allowlist gaps this server surfaces are network-INDEPENDENT anyway — `eventfd`
    (Tokio runtime, round 1) and `flock` (uv cache lock, round 2) are denied
    regardless of the network flag, which is why they are the completeness
    signal.

    ⚠ Skip discipline (#3059 co-vet — a skip must not MASK an allowlist gap).
    Skips ONLY on genuine environment-reachability failure (uvx absent, PyPI
    fetch blocked). A failure whose signature is a SANDBOX DENIAL — a seccomp
    EPERM that stops the runtime/cache-lock (`Operation not permitted` /
    `PermissionDenied` / `socketpair` / `failed to lock` / Tokio "Failed building
    the Runtime") — is the exact #2962 completeness bug this test exists to
    catch, so it must FAIL, not skip. The first #3059 CI run skipped here on a
    Tokio `eventfd` denial while telling the operator to "set network: true" — a
    local IPC denial mis-reported as a network problem. This guard makes that a
    hard failure.
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
        "network": True,  # isolate allowlist completeness from uv's own net use
        "subprocess": True,
        "cwd": str(tmp_path),
    }
    # A sandbox-denial signature = a seccomp EPERM killing the async runtime =
    # the #2962 completeness bug. It must NOT be skipped as a network issue.
    _SANDBOX_DENIAL_MARKERS = (
        "Operation not permitted",
        "PermissionDenied",
        "PermissionError",
        "socketpair",
        "Failed building the Runtime",  # Tokio, on an eventfd/similar denial
        "failed to lock",  # uv cache flock denied (round-2 gap)
    )
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
        msg = repr(exc)
        if any(marker in msg for marker in _SANDBOX_DENIAL_MARKERS):
            pytest.fail(
                f"markitdown-mcp was DENIED a syscall by the seccomp allowlist — "
                f"a completeness gap (#2962's class), NOT a network problem, so "
                f"it must fail rather than skip and must NOT tell the operator to "
                f"enable network. Add the denied local-IPC syscall to `_BASELINE`: "
                f"{msg}"
            )
        pytest.skip(
            f"markitdown-mcp unreachable for a non-sandbox reason "
            f"(uvx/PyPI/transport env), orthogonal to allowlist completeness: {msg}"
        )
    assert not result.get("isError"), (
        f"markitdown-mcp's real 'convert_to_markdown' tool call FAILED under "
        f"the allowlist — an allowlist gap silently broke a real MCP server: "
        f"{result!r}"
    )


# ── FastMCP telemetry env reaches the builtin RAG server's spawn point (#3060) ─
#
# The other half of #3060: FastMCP's own `run_stdio_async(show_banner=True)`
# default logs a banner that calls `check_for_newer_version()` — a REAL
# outbound `httpx.get("https://pypi.org/pypi/fastmcp/json")` — on every
# builtin chunker/vector-store server start, independent of the sandbox
# network gate (this call happens from the server's own process, which is
# under `network: true` by default for these two servers — see the "why
# network=True" note above — so the sandbox never sees it). The fix disables
# it via env, set in the plugin's own ``.mcp.json`` launch config (not
# ``resolve_passthrough_env``, which is the generic proxy/CA-env union, not
# server-specific). This witnesses the env reaches the REAL production spawn
# path — ``_build_mcp_entries`` is the exact function ``_register_mcp``
# (``plugin_install.py``) calls to turn ``.mcp.json`` into the
# ``mcp.servers.<name>`` entries ``MCPClient`` reads ``env`` from
# (``client.py``'s ``_open_stdio``) — fed the REAL shipped ``.mcp.json``, no
# fakes.


def test_rag_plugin_mcp_json_disables_fastmcp_telemetry_env() -> None:
    """Tier 2c: the builtin ``rag`` plugin's ``.mcp.json`` declares
    ``FASTMCP_SHOW_SERVER_BANNER=false`` / ``FASTMCP_CHECK_FOR_UPDATES=off``
    for both servers, and the REAL ``_build_mcp_entries`` (the production
    function that turns ``.mcp.json`` into the ``mcp.servers.<name>`` entries
    written to ``.reyn/config/mcp.yaml`` and read by ``MCPClient``'s
    ``_open_stdio``) carries those env vars through into the entry ``env``
    dict unchanged — the actual subprocess-spawn env dict, not just the
    source JSON file."""
    import reyn.builtin as _builtin_pkg
    from reyn.core.op_runtime.plugin_install import _build_mcp_entries

    mcp_json = (
        Path(_builtin_pkg.__file__).resolve().parent / "plugins" / "rag" / ".mcp.json"
    )
    assert mcp_json.exists(), f"builtin rag plugin .mcp.json not found at {mcp_json}"

    entries = _build_mcp_entries(mcp_json)
    assert set(entries) == {"reyn_chunker", "reyn_vector_store"}, (
        f"unexpected server set in .mcp.json: {sorted(entries)}"
    )
    for name, entry in entries.items():
        env = entry.get("env")
        assert env is not None, (
            f"{name!r}'s mcp entry carries no 'env' at all — the telemetry-"
            f"disabling vars did not survive _build_mcp_entries"
        )
        assert env.get("FASTMCP_SHOW_SERVER_BANNER") == "false", (
            f"{name!r}: FASTMCP_SHOW_SERVER_BANNER must reach the spawn env "
            f"as 'false' (FastMCP env vars are string-typed); got {env!r}"
        )
        assert env.get("FASTMCP_CHECK_FOR_UPDATES") == "off", (
            f"{name!r}: FASTMCP_CHECK_FOR_UPDATES must reach the spawn env "
            f"as 'off'; got {env!r}"
        )
