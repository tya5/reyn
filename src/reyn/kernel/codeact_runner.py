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
from pathlib import Path
from typing import Any, Awaitable, Callable


def _harness_subprocess_env() -> dict[str, str]:
    """Env for the harness subprocess with the PARENT process's reyn tree propagated
    onto PYTHONPATH (#1609). Without this, ``python -m reyn.kernel._codeact_harness``
    resolves ``reyn`` from the spawned interpreter's default ``sys.path`` — which in a
    multi-worktree editable-install dev env can point at a DIFFERENT worktree lacking
    this harness module (``No module named reyn.kernel._codeact_harness``). Prepending
    this process's reyn tree makes the subprocess resolve the SAME tree. Production
    (single reyn install) is unaffected — same path either way. (The codeact harness
    interpreter is always the host ``sys.executable`` — #1663; it does NOT honor
    ``REYN_HARNESS_PYTHON`` (unlike the preprocessor harness), so this PYTHONPATH
    propagation pairs with that host interpreter.)"""
    import reyn  # noqa: PLC0415

    tree = str(Path(reyn.__file__).resolve().parent.parent)  # dir containing the reyn pkg
    env = dict(os.environ)
    existing = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = tree + (os.pathsep + existing if existing else "")
    return env

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
        # #1663: the CodeAct harness is a HOST-LOCAL orchestrator — its AF_UNIX
        # control socket is passed to the child via ``pass_fds`` (an inherited fd
        # cannot cross a ``docker exec`` boundary), so the harness must run on the
        # reyn host under the reyn-process interpreter. It deliberately does NOT
        # honor ``REYN_HARNESS_PYTHON``: that override targets the in-container
        # #1356 *preprocessor* harness (PythonRunner), which is routed through
        # ``backend.run`` (= ``docker exec``) and so needs the container's python.
        # Picking it up here pointed codeact's host Popen at a container-only path
        # (``/opt/reyn-venv/bin/python``) under ``--env-backend=docker`` → the
        # seatbelt-wrapped exec failed with execvp rc=71. Tool EFFECTS still reach
        # the container via the gated dispatch (DockerEnvironmentBackend), so the
        # host-local harness loses nothing. An explicit arg still wins (tests).
        self.python_executable = python_executable or sys.executable

    async def run(
        self,
        *,
        code: str,
        dispatch: DispatchFn,
        actions: "dict[str, str] | None" = None,  # #1658 {identifier: qualified_name}
        sandbox_backend: Any = None,
        sandbox_policy: dict | None = None,
        allowed_modules: list[str] | None = None,
        timeout: float = 30.0,
        cwd: str | None = None,
        allow_unsandboxed: bool = False,
    ) -> dict[str, Any]:
        """Execute ``code`` in the CodeAct harness; service its tool() proxy via
        ``dispatch``. Returns the harness response dict
        (``{ok: True, result}`` | ``{ok: False, kind, error, traceback?}``), plus a
        ``status`` field (``ok`` | ``error`` | ``timeout`` | ``sandbox_unavailable``)
        for the scheme layer.

        **Fail-closed** (owner-signed): CodeAct runs ONLY under an available OS
        sandbox (Seatbelt / Landlock). When no real backend is available the run is
        refused (``sandbox_unavailable``), never silently downgraded to an
        unsandboxed subprocess. ``allow_unsandboxed=True`` is a **test-only** escape
        for exercising the transport/proxy core without a sandbox; production callers
        (the CodeAct scheme) never set it.

        S2b wires the Seatbelt wrap (reusing ``_build_sbpl_profile``); S2c wires
        Landlock.
        """
        base_argv = [self.python_executable, "-m", "reyn.kernel._codeact_harness"]
        argv, cleanup, spawn_error = self._resolve_sandbox_spawn(
            base_argv, sandbox_backend, sandbox_policy, timeout, allow_unsandboxed,
        )
        if spawn_error is not None:
            return {
                "ok": False, "status": "sandbox_unavailable",
                "kind": "SandboxUnavailable", "error": spawn_error,
            }

        parent_sock, child_sock = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
        child_fd = child_sock.fileno()
        os.set_inheritable(child_fd, True)

        request = {
            "code": code,
            "control_fd": child_fd,
            # #1658: {identifier: qualified_name} — the harness injects a gated direct-
            # function stub per identifier (each marshals the REAL qualified name over
            # the control channel to the parent gate). Empty → no direct functions
            # (back-compat: the snippet can still use the internal tool() primitive).
            "actions": dict(actions or {}),
            "allowed_modules": list(allowed_modules or []),
        }

        try:
            proc = subprocess.Popen(  # noqa: S603 — fixed argv, sandbox-wrapped above
                argv,
                pass_fds=[child_fd],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd=cwd,
                env=_harness_subprocess_env(),  # #1609: parent reyn tree on PYTHONPATH
                start_new_session=True,
            )
        except OSError as exc:
            child_sock.close()
            parent_sock.close()
            if cleanup is not None:
                cleanup()
            return {"ok": False, "status": "error", "kind": "SpawnError", "error": str(exc)}

        # The child inherited its own copy of the fd; the parent keeps only its end.
        child_sock.close()

        loop = asyncio.get_running_loop()
        parent_sock.setblocking(False)
        # #1618 root-2: the snippet's result arrives as an op="final" frame on the
        # control channel (not stdout); _service captures it here.
        final_box: list[dict] = []
        service_task = asyncio.create_task(
            self._service(parent_sock, dispatch, loop, final_box)
        )

        # ``communicate(input=...)`` writes the request to stdin (the child reads it
        # fully before touching the control channel), then reads stdout/stderr +
        # waits. It runs in an executor thread, so the ``service_task`` services the
        # control channel concurrently on the event loop while the child blocks on a
        # mid-execution tool() call.
        request_bytes = json.dumps(request).encode("utf-8")
        comm_future = loop.run_in_executor(
            None, lambda: proc.communicate(input=request_bytes),
        )
        timed_out = False
        try:
            stdout_b, stderr_b = await asyncio.wait_for(comm_future, timeout=timeout)
        except asyncio.TimeoutError:
            timed_out = True
            service_task.cancel()
            await _kill_proc_group(proc, loop)
            stdout_b, stderr_b = b"", b""
        else:
            # Normal exit: the child sent op="final" then closed the channel (EOF), so
            # DRAIN the service task (bounded) to populate final_box, rather than
            # cancelling it mid-frame.
            try:
                await asyncio.wait_for(service_task, timeout=2.0)
            except Exception:  # noqa: BLE001 — drain best-effort; cancel if it hangs
                service_task.cancel()
        finally:
            try:
                parent_sock.close()
            except OSError:
                pass
            if cleanup is not None:
                cleanup()

        if timed_out:
            return {
                "ok": False, "status": "timeout",
                "kind": "Timeout", "error": f"codeact timed out after {timeout}s",
            }
        # #1618 root-2: the result is the op="final" frame; stdout/stderr are now PURE
        # user-program output, captured as data (the format_feedback fallback when the
        # snippet print()s instead of binding ``result``). No final frame = an early
        # crash before the channel opened → the stdout crash-path fallback.
        if final_box:
            final = dict(final_box[0])
            final.pop("op", None)
            final["stdout"] = (stdout_b or b"").decode("utf-8", errors="replace")
            final["stderr"] = (stderr_b or b"").decode("utf-8", errors="replace")
            return final
        return self._parse_response(stdout_b, stderr_b, proc.returncode)

    def _resolve_sandbox_spawn(
        self,
        base_argv: list[str],
        sandbox_backend: Any,
        sandbox_policy: dict | None,
        timeout: float,
        allow_unsandboxed: bool,
    ) -> tuple[list[str] | None, Callable[[], None] | None, str | None]:
        """Resolve the spawn argv + a cleanup callable for the active sandbox, or an
        error string (fail-closed). Returns ``(argv, cleanup, error)``; exactly one
        of ``argv`` / ``error`` is non-None.

        - Seatbelt (available): wrap ``base_argv`` with ``sandbox-exec -f <profile>``
          using the REUSED ``_build_sbpl_profile`` (the inherited socketpair fd
          survives — an AF_UNIX socketpair is not a ``network*`` socket).
        - Landlock (available): S2c (preexec ruleset) — not yet wired → fail-closed.
        - noop / None / unavailable: fail-closed unless ``allow_unsandboxed`` (a
          test-only escape for the transport/proxy core).
        """
        name = getattr(sandbox_backend, "name", None)
        available = bool(sandbox_backend is not None and sandbox_backend.available())

        if sandbox_backend is None or name in (None, "noop") or not available:
            if allow_unsandboxed:
                return base_argv, None, None
            return None, None, (
                "CodeAct requires an available OS sandbox backend (Seatbelt / "
                "Landlock); none available — refusing to run unsandboxed (fail-closed)."
            )

        from reyn.sandbox import SandboxPolicy  # noqa: PLC0415

        policy_dict = dict(sandbox_policy or {})
        policy_dict["timeout_seconds"] = timeout
        policy = SandboxPolicy(**policy_dict)

        if name == "seatbelt":
            import tempfile  # noqa: PLC0415

            from reyn.sandbox.backends.seatbelt import (  # noqa: PLC0415
                _build_sbpl_profile,
            )

            profile_text = _build_sbpl_profile(policy)
            try:
                fh = tempfile.NamedTemporaryFile(
                    suffix=".sb", mode="w", delete=False, encoding="utf-8",
                )
                fh.write(profile_text)
                fh.close()
            except OSError as exc:
                return None, None, f"CodeAct: failed to write Seatbelt profile: {exc}"

            def _cleanup() -> None:
                try:
                    os.unlink(fh.name)
                except OSError:
                    pass

            return ["sandbox-exec", "-f", fh.name, *base_argv], _cleanup, None

        if name == "landlock":
            return None, None, (
                "CodeAct Landlock wrap is S2c (pending Linux+Landlock env "
                "verification) — not yet available (fail-closed)."
            )

        return None, None, (
            f"CodeAct does not support sandbox backend {name!r} yet (fail-closed)."
        )

    async def _service(
        self, sock: socket.socket, dispatch: DispatchFn, loop: asyncio.AbstractEventLoop,
        final_box: list[dict],
    ) -> None:
        """Service the control channel until the child closes it (EOF). Each
        ``tool_call`` is gated by ``dispatch`` (the parent's exclude + dispatch_tool
        + permission pipeline) and the result envelope is sent back. The terminal
        ``op="final"`` frame (#1618 root-2: the snippet's result, now on this channel
        instead of stdout) is captured into ``final_box`` — no reply, the child exits."""
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
                op = req.get("op")
                if op == "final":
                    final_box.append(req)  # the snippet's result envelope
                    continue
                if op != "tool_call":
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
