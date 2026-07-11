"""Tier 2: sandbox launcher-fork denial classification + canonical note (#2820, B).

The denial classifier is a pure function of ``(returncode, stderr)`` — these lock
it down by static replay over the REAL captured stderr (``pyenv: fork: Operation
not permitted``, the ../user incident's exact bytes), never by re-running a
sandbox. The canonical tests prove the opaque raw stderr is turned into an
explicit "environment/config, not tool-availability" note — the whole point of
part B is to stop a weak model reading the raw line as "I cannot execute tools".
"""
from __future__ import annotations

import pytest

from reyn.core.offload.canonical import sandboxed_exec_to_canonical
from reyn.security.sandbox.backend import SandboxResult
from reyn.security.sandbox.denial import DENIAL_FORK, classify_denial

# The exact stderr the incident produced (bare ``python3`` → pyenv shim → pyenv
# forks under (deny process-fork)). Captured, not synthesized.
_REAL_FORK_STDERR = b"/opt/homebrew/opt/pyenv/bin/pyenv: fork: Operation not permitted\n"


def test_classify_real_captured_fork_denial_stderr():
    """Tier 2: the real ../user incident stderr classifies as fork_denied."""
    assert classify_denial(128, _REAL_FORK_STDERR) == DENIAL_FORK


def test_classify_eagain_variant():
    """Tier 2: the Linux EAGAIN wording is the same class (a seccomp/rlimit
    surface of the identical launcher-fork denial)."""
    assert (
        classify_denial(1, b"npx: fork: Resource temporarily unavailable")
        == DENIAL_FORK
    )


def test_classify_is_case_insensitive():
    """Tier 2: signature match must not hinge on exact casing of the OS message."""
    assert classify_denial(128, b"sh: FORK: OPERATION NOT PERMITTED") == DENIAL_FORK


def test_zero_returncode_is_never_a_denial():
    """Tier 2: a success is never a denial even if output coincidentally matches —
    the classifier gates on failure first."""
    assert classify_denial(0, _REAL_FORK_STDERR) is None


def test_ordinary_nonzero_failure_is_not_classified():
    """Tier 2: a normal command failure (no fork-denial signature) → None, so the
    canonical note only fires on the real thing."""
    assert classify_denial(1, b"ls: nope: No such file or directory") is None
    assert classify_denial(2, b"") is None


def test_canonical_prepends_env_not_tool_note_on_fork_denial():
    """Tier 2: a fork_denied result renders the explicit environment-vs-tool note
    (naming the resolved shim) AND carries denial_class in meta — the LLM must see
    "NOT a lack of tool-calling ability", not just the raw pyenv line."""
    result = {
        "kind": "sandboxed_exec",
        "status": "error",
        "returncode": 128,
        "stdout": "",
        "stderr": _REAL_FORK_STDERR.decode(),
        "denial_class": DENIAL_FORK,
        "argv0_resolved": "/Users/x/.pyenv/shims/python3",
    }
    canonical = sandboxed_exec_to_canonical(result)
    text = canonical["text"]
    assert "NOT a lack of tool-calling ability" in text
    assert "environment" in text.lower()
    assert "/Users/x/.pyenv/shims/python3" in text  # names the resolved shim
    assert "pyenv" in text  # the raw stderr is still present below the note
    assert canonical["meta"].get("denial_class") == DENIAL_FORK


def test_canonical_note_absent_on_ordinary_failure():
    """Tier 2: regression guard — a normal nonzero exit (denial_class None) gets
    NO launcher note and NO denial_class meta; only returncode signal survives."""
    result = {
        "kind": "sandboxed_exec",
        "status": "error",
        "returncode": 2,
        "stdout": "",
        "stderr": "boom",
        "denial_class": None,
        "argv0_resolved": "/usr/bin/false",
    }
    canonical = sandboxed_exec_to_canonical(result)
    assert "[sandbox]" not in canonical["text"]
    assert "denial_class" not in canonical["meta"]
    assert canonical["meta"].get("returncode") == 2


class _ForkDenyingBackend:
    """Real SandboxBackend test double (NOT a mock) that returns the captured
    fork-denial result — proves the handler classifies + surfaces it end-to-end."""

    name = "fake-forkdeny"

    def available(self) -> bool:
        return True

    def wrap_command(self, argv, policy):  # pragma: no cover - unused here
        from reyn.security.sandbox.backend import WrappedCommand

        return WrappedCommand(argv=list(argv))

    async def run(self, argv, policy, *, stdin=None, cwd=None, cancel_event=None):
        return SandboxResult(returncode=128, stdout=b"", stderr=_REAL_FORK_STDERR)


@pytest.mark.asyncio
async def test_handler_surfaces_denial_class_and_argv0_end_to_end():
    """Tier 2: the real handler, given a backend that reproduces the fork denial,
    returns denial_class='fork_denied' in the P5 dict AND emits it (with
    argv0_resolved) on the P6 sandboxed_exec_completed event — the production
    wiring, not just the pure fn."""
    from reyn.core.events.events import EventLog
    from reyn.core.op_runtime import execute_op
    from reyn.core.op_runtime.context import OpContext
    from reyn.data.workspace.workspace import Workspace
    from reyn.schemas.models import SandboxedExecIROp
    from reyn.security.permissions.permissions import PermissionDecl

    events = EventLog()
    workspace = Workspace(events=events)
    ctx = OpContext(
        workspace=workspace,
        events=events,
        permission_decl=PermissionDecl(),
        sandbox_backend=_ForkDenyingBackend(),
    )
    op = SandboxedExecIROp(
        kind="sandboxed_exec",
        argv=["python3", "-c", "print(2+2)"],
        env_passthrough=["PATH"],
        timeout_seconds=30,
    )

    result = await execute_op(op, ctx)

    assert result["denial_class"] == DENIAL_FORK
    assert "argv0_resolved" in result  # present (value may be None if python3 off PATH)

    completed = [e for e in events.all() if e.type == "sandboxed_exec_completed"]
    assert completed, "sandboxed_exec_completed not emitted"
    assert completed[0].data.get("denial_class") == DENIAL_FORK
    assert "argv0_resolved" in completed[0].data
