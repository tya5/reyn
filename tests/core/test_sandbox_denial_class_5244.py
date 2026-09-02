"""Tier 2: sandbox network-connect denial classification + canonical note
(#5244 ①).

Same shape as :mod:`test_sandbox_denial_class_2820` (fork denial) — the
denial classifier is a pure function of ``(returncode, stderr)``, locked
down by static replay over REAL captured stderr, never by re-running a
sandbox. Captured directly on this machine (macOS seatbelt, #5244
investigation): a raw ``socket.connect()`` EPERM, the SAME error still
present verbatim inside an asyncio ``TaskGroup``'s own ``ExceptionGroup``
aggregation (the actual real-machine incident's shape), and — for
contrast — a write-path denial's own EPERM (same errno, must NOT
classify as network).
"""
from __future__ import annotations

import pytest

from reyn.core.offload.canonical import sandboxed_exec_to_canonical
from reyn.security.sandbox.backend import SandboxResult
from reyn.security.sandbox.denial import DENIAL_NETWORK, classify_denial
from tests._support.events import collect_events, settle
from tests._support.sandbox_backend import FULLY_ENFORCING_AXES

# Captured directly (#5244 investigation, macOS seatbelt, network=False):
# a raw `socket.connect()` under a sandboxed Python subprocess.
_REAL_NETWORK_STDERR = (
    b'Traceback (most recent call last):\n'
    b'  File "<string>", line 3, in <module>\n'
    b'PermissionError: [Errno 1] Operation not permitted\n'
)

# Captured directly — the SAME underlying denial, but reached through an
# asyncio TaskGroup (the real-machine #5244 shape: an MCP client's own
# ExceptionGroup wrapping). The raw PermissionError line survives the
# wrapping verbatim.
_REAL_NETWORK_STDERR_TASKGROUP = (
    b'  + Exception Group Traceback (most recent call last):\n'
    b'  |   File "<string>", line 7, in <module>\n'
    b'  |   File ".../asyncio/taskgroups.py", line 145, in __aexit__\n'
    b'  |     raise me from None\n'
    b'  | ExceptionGroup: unhandled errors in a TaskGroup (1 sub-exception)\n'
    b'  +-+---------------- 1 ----------------\n'
    b'    | Traceback (most recent call last):\n'
    b'    |   File "<string>", line 3, in conn\n'
    b'    | PermissionError: [Errno 1] Operation not permitted\n'
    b'    +------------------------------------\n'
)

# Captured directly for CONTRAST — a write-path denial under the SAME
# errno (EPERM=1). Python's OSError includes a trailing `: '<path>'` only
# when its own `filename` argument is set (a FILE op); a socket connect()
# error never sets it. This is what the classifier must NOT match.
_REAL_WRITE_DENY_STDERR = (
    b"Traceback (most recent call last):\n"
    b'  File "<string>", line 1, in <module>\n'
    b"PermissionError: [Errno 1] Operation not permitted: "
    b"'/tmp/definitely-denied-reyn-test.txt'\n"
)


def test_classify_real_captured_raw_connect_denial_stderr():
    """Tier 2: the real captured raw-connect() stderr classifies as
    network_denied."""
    assert classify_denial(1, _REAL_NETWORK_STDERR) == DENIAL_NETWORK


def test_classify_real_captured_taskgroup_wrapped_denial_stderr():
    """Tier 2: the real-machine #5244 shape — the SAME denial, buried
    inside an asyncio ExceptionGroup — still classifies correctly. This
    is the actual incident shape (an MCP client's own TaskGroup), not
    just the raw synthetic case above."""
    assert classify_denial(1, _REAL_NETWORK_STDERR_TASKGROUP) == DENIAL_NETWORK


def test_classify_does_not_confuse_a_write_deny_for_a_network_deny():
    """Tier 2: control arm — a write-path denial shares the SAME errno
    (EPERM=1) as a network denial, but must NOT classify as
    network_denied (misclassifying it would tell an operator to set
    `network: true` when the real fix is `write_paths`)."""
    assert classify_denial(1, _REAL_WRITE_DENY_STDERR) is None


def test_classify_is_case_insensitive():
    """Tier 2: signature match must not hinge on exact casing."""
    assert (
        classify_denial(1, b"PERMISSIONERROR: [ERRNO 1] OPERATION NOT PERMITTED")
        == DENIAL_NETWORK
    )


def test_zero_returncode_is_never_a_denial():
    """Tier 2: a success is never a denial even if output coincidentally
    matches — the classifier gates on failure first."""
    assert classify_denial(0, _REAL_NETWORK_STDERR) is None


def test_canonical_prepends_env_not_tool_note_on_network_denial():
    """Tier 2: a network_denied result renders the explicit
    environment-vs-tool note AND carries denial_class in meta — the LLM
    must see "NOT a lack of tool-calling ability", not just the raw
    ExceptionGroup traceback."""
    result = {
        "kind": "sandboxed_exec",
        "status": "error",
        "returncode": 1,
        "stdout": "",
        "stderr": _REAL_NETWORK_STDERR_TASKGROUP.decode(),
        "denial_class": DENIAL_NETWORK,
    }
    canonical = sandboxed_exec_to_canonical(result)
    text = canonical["text"]
    assert "NOT a lack of tool-calling ability" in text
    assert "network: true" in text
    assert "ExceptionGroup" in text  # the raw stderr is still present below the note
    assert canonical["meta"].get("denial_class") == DENIAL_NETWORK


class _NetworkDenyingBackend:
    """Real SandboxBackend test double (NOT a mock) that returns the
    captured network-denial result — proves the handler classifies +
    surfaces it end-to-end."""

    name = "fake-networkdeny"
    enforced_axes = FULLY_ENFORCING_AXES

    def available(self) -> bool:
        return True

    def wrap_command(self, argv, policy):  # pragma: no cover - unused here
        from reyn.security.sandbox.backend import WrappedCommand

        return WrappedCommand(argv=list(argv), env={})

    async def run(self, argv, policy, *, stdin=None, cwd=None, cancel_event=None, hook_process_context=None, sink=None):
        return SandboxResult(
            returncode=1, stdout=b"", stderr=_REAL_NETWORK_STDERR_TASKGROUP,
        )


@pytest.mark.asyncio
async def test_handler_surfaces_denial_class_end_to_end():
    """Tier 2: the real handler, given a backend that reproduces the
    network denial, returns denial_class='network_denied' in the P5 dict
    AND emits it on the P6 sandboxed_exec_completed event — the
    production wiring, not just the pure fn."""
    from reyn.core.events.events import EventLog
    from reyn.core.op_runtime import execute_op
    from reyn.core.op_runtime.context import OpContext
    from reyn.data.workspace.workspace import Workspace
    from reyn.schemas.models import SandboxedExecIROp
    from reyn.security.permissions.permissions import PermissionDecl

    events = EventLog()
    collected = collect_events(events)
    workspace = Workspace(events=events)
    ctx = OpContext(
        workspace=workspace,
        events=events,
        permission_decl=PermissionDecl(),
        sandbox_backend=_NetworkDenyingBackend(),
        default_sandbox_policy={},
    )
    op = SandboxedExecIROp(
        kind="sandboxed_exec",
        argv=["python3", "-c", "print(2+2)"],
    )

    result = await execute_op(op, ctx)

    assert result["denial_class"] == DENIAL_NETWORK

    await settle(events)
    completed = [e for e in collected if e.type == "sandboxed_exec_completed"]
    assert completed, "sandboxed_exec_completed not emitted"
    assert completed[0].data.get("denial_class") == DENIAL_NETWORK
