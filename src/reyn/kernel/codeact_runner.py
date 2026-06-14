"""Parent-side orchestrator for a CodeAct snippet (#1593 PR-3, S2).

Runs the model's snippet in a subprocess (``reyn.kernel._codeact_harness``) and
services its duplex permission-proxy: each ``tool(name, **args)`` the snippet calls
round-trips over an inherited AF_UNIX socketpair to ``dispatch`` here in the parent
— the SAME OS exclude + ``dispatch_tool`` + permission gate (P5). The snippet holds
no permission authority and cannot reach Reyn internals; the socket is the single,
audited hole carrying only marshalled tool calls.

Why a dedicated runner (not ``SandboxBackend.run``): ``run`` is single-shot
(``subprocess.run`` capture — stdout read only after exit), but CodeAct needs a
duplex channel **live during execution**. The runner does its own
``Popen(pass_fds=...)`` so the socketpair fd is inherited, and services the channel
concurrently with the child. The OS sandbox is applied by reusing the backend's
profile builder (S2b: Seatbelt ``_build_sbpl_profile`` + ``sandbox-exec`` wrapper;
S2c: Landlock preexec ruleset) — the inherited socketpair fd survives both (an
AF_UNIX socketpair is not a ``network*`` socket; verified on Seatbelt under
``(deny default)+(deny network*)``).

This module is the S2a core: the protocol + service loop + the direct (no-sandbox)
spawn. The sandbox-wrapping spawn lands in S2b/S2c.
"""
from __future__ import annotations

import asyncio
import json
import os
import signal
import socket
import subprocess
import sys
from typing import Any, Awaitable, Callable

# A dispatch callback: (name, args) -> the dispatch_tool result envelope
# ({"status": "ok", "data": ...} | {"status": "error", "error": {...}}). The
# CodeAct scheme (S3) supplies one that runs the OS exclude gate + dispatch_tool;
# tests supply a real callback (no mocks).
DispatchFn = Callable[[str, dict], Awaitable[dict]]


class CodeActRunner:
    """Run a CodeAct snippet with a duplex permission-proxy to the parent gate.

    Stateless aside from ``python_executable``; one runner serves many snippets.
    """

    def __init__(self, python_executable: str | None = None) -> None:
        self.python_executable = (
            python_executable
            or os.environ.get("REYN_HARNESS_PYTHON")
            or sys.executable
        )

    async def run(
        self,
        *,
        code: str,
        dispatch: DispatchFn,
        allowed_modules: list[str] | None = None,
        timeout: float = 30.0,
        cwd: str | None = None,
    ) -> dict[str, Any]:
        """Execute ``code`` in the CodeAct harness; service its tool() proxy via
        ``dispatch``. Returns the harness response dict
        (``{ok: True, result}`` | ``{ok: False, kind, error, traceback?}``), plus a
        ``status`` field (``ok`` | ``error`` | ``timeout``) for the scheme layer.

        S2a: direct (no-sandbox) spawn. S2b/S2c wrap the spawn in the OS sandbox.
        """
        parent_sock, child_sock = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
        child_fd = child_sock.fileno()
        os.set_inheritable(child_fd, True)

        request = {
            "code": code,
            "control_fd": child_fd,
            "allowed_modules": list(allowed_modules or []),
        }
        argv = [self.python_executable, "-m", "reyn.kernel._codeact_harness"]

        try:
            proc = subprocess.Popen(  # noqa: S603 — fixed argv, sandbox wrap in S2b
                argv,
                pass_fds=[child_fd],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd=cwd,
                start_new_session=True,
            )
        except OSError as exc:
            child_sock.close()
            parent_sock.close()
            return {"ok": False, "status": "error", "kind": "SpawnError", "error": str(exc)}

        # The child inherited its own copy of the fd; the parent keeps only its end.
        child_sock.close()

        loop = asyncio.get_running_loop()
        parent_sock.setblocking(False)
        service_task = asyncio.create_task(self._service(parent_sock, dispatch, loop))

        # ``communicate(input=...)`` writes the request to stdin (the child reads it
        # fully before touching the control channel), then reads stdout/stderr +
        # waits. It runs in an executor thread, so the ``service_task`` services the
        # control channel concurrently on the event loop while the child blocks on a
        # mid-execution tool() call.
        request_bytes = json.dumps(request).encode("utf-8")
        comm_future = loop.run_in_executor(
            None, lambda: proc.communicate(input=request_bytes),
        )
        try:
            stdout_b, stderr_b = await asyncio.wait_for(comm_future, timeout=timeout)
        except asyncio.TimeoutError:
            await _kill_proc_group(proc, loop)
            return {
                "ok": False, "status": "timeout",
                "kind": "Timeout", "error": f"codeact timed out after {timeout}s",
            }
        finally:
            service_task.cancel()
            try:
                parent_sock.close()
            except OSError:
                pass

        return self._parse_response(stdout_b, stderr_b, proc.returncode)

    async def _service(
        self, sock: socket.socket, dispatch: DispatchFn, loop: asyncio.AbstractEventLoop,
    ) -> None:
        """Service the control channel until the child closes it (EOF). Each
        ``tool_call`` is gated by ``dispatch`` (the parent's exclude + dispatch_tool
        + permission pipeline) and the result envelope is sent back."""
        buf = b""
        while True:
            try:
                chunk = await loop.sock_recv(sock, 65536)
            except (ConnectionError, OSError):
                return
            if not chunk:
                return  # child closed the channel — snippet finished
            buf += chunk
            while b"\n" in buf:
                line, _, buf = buf.partition(b"\n")
                if not line:
                    continue
                req = json.loads(line.decode("utf-8"))
                if req.get("op") != "tool_call":
                    continue
                result = await dispatch(req.get("name", ""), req.get("args", {}) or {})
                reply = json.dumps({"op": "result", "result": result}).encode("utf-8")
                try:
                    await loop.sock_sendall(sock, reply + b"\n")
                except (ConnectionError, OSError):
                    return

    def _parse_response(
        self, stdout_b: bytes, stderr_b: bytes, returncode: int | None,
    ) -> dict[str, Any]:
        stdout_text = (stdout_b or b"").decode("utf-8", errors="replace")
        if not stdout_text.strip():
            stderr_text = (stderr_b or b"").decode("utf-8", errors="replace")
            return {
                "ok": False, "status": "error", "kind": "Crash",
                "error": f"codeact harness produced no output (rc={returncode}): "
                         f"{stderr_text.strip()[:300]}",
            }
        try:
            payload = json.loads(stdout_text)
        except json.JSONDecodeError:
            return {
                "ok": False, "status": "error", "kind": "MalformedResponse",
                "error": f"codeact harness returned malformed JSON: {stdout_text[:300]}",
            }
        payload["status"] = "ok" if payload.get("ok") else "error"
        return payload


async def _kill_proc_group(
    proc: subprocess.Popen, loop: asyncio.AbstractEventLoop, grace_seconds: float = 2.0,
) -> None:
    """SIGTERM the process group, then SIGKILL after grace if still alive (mirrors
    the SeatbeltBackend cancel kill — covers the wrapper + child under the sandbox)."""
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
    except (ProcessLookupError, OSError):
        return
    try:
        await asyncio.wait_for(
            loop.run_in_executor(None, proc.wait), timeout=grace_seconds,
        )
    except (asyncio.TimeoutError, Exception):
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except (ProcessLookupError, OSError):
            pass
