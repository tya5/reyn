"""Parent-side orchestrator for a CodeAct snippet (#1593 PR-3, S2).

Runs the model's snippet in a subprocess (``reyn.core.kernel._codeact_harness``) and
services its duplex permission-proxy: each ``tool(name, /, **args)`` the snippet calls
round-trips over an inherited AF_UNIX socketpair to ``dispatch`` here in the parent
— the SAME OS exclude + ``dispatch_tool`` + permission gate (P5). The snippet holds
no permission authority and cannot reach Reyn internals; the socket is the single,
audited hole carrying only marshalled tool calls.

Why a dedicated runner (not ``SandboxBackend.run``): ``run`` is single-shot
(``subprocess.run`` capture — stdout read only after exit), but CodeAct needs a
duplex channel **live during execution**. The runner does its own
``Popen(pass_fds=...)`` so the socketpair fd is inherited, and services the channel
concurrently with the child. The OS sandbox is applied via the SAME
``SandboxBackend.wrap_command(argv, policy)`` abstraction every other
command-level launch route uses (#2626, #2628) — Seatbelt's ``sandbox-exec -f
<profile>`` wrap or Landlock's re-exec shim, whichever backend is available —
so the inherited socketpair fd survives both (an AF_UNIX socketpair is not a
``network*`` socket; verified on Seatbelt under ``(deny default)+(deny
network*)``).

This module is the S2a core: the protocol + service loop + the direct (no-sandbox)
spawn, plus the ``wrap_command``-delegated sandboxed spawn.
"""
from __future__ import annotations

import asyncio
import json
import os
import socket
import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any, Awaitable, Callable

from reyn.security.sandbox import kill_process_tree
from reyn.security.sandbox._subprocess_io import communicate_capped
from reyn.security.sandbox.policy import POST_KILL_DRAIN_GRACE_SECONDS

if TYPE_CHECKING:
    from reyn.security.sandbox import SandboxPolicy


def _harness_subprocess_env(policy: "SandboxPolicy") -> dict[str, str]:
    """Env for the harness subprocess: the SAME allowlist chokepoint
    (:func:`resolve_passthrough_env`) every other sandboxed launch route uses
    (#3075 fix 5), not a bespoke full-environ copy (#3822 — CodeAct was the one
    seam still doing ``dict(os.environ)``, handing a model-authored snippet
    every parent env var — secrets included — while the sibling
    ``SandboxBackend.run`` seam already went through the allowlist).

    On top of that base, the PARENT process's reyn tree is prepended onto
    PYTHONPATH (#1609). Without this, ``python -m
    reyn.core.kernel._codeact_harness`` resolves ``reyn`` from the spawned
    interpreter's default ``sys.path`` — which in a multi-worktree
    editable-install dev env can point at a DIFFERENT worktree lacking this
    harness module (``No module named reyn.core.kernel._codeact_harness``).
    Prepending this process's reyn tree makes the subprocess resolve the SAME
    tree. Production (single reyn install) is unaffected — same path either
    way. (The codeact harness interpreter is always the host
    ``sys.executable`` — #1663; it does NOT honor ``REYN_HARNESS_PYTHON``
    (unlike the preprocessor harness), so this PYTHONPATH propagation pairs
    with that host interpreter.) PYTHONPATH passes through by the env-compat
    default (#3901 PR-B ④, owner ruling B) — ``_resolve_sandbox_spawn`` no
    longer needs to force it into an allowlist (there is no longer an
    allowlist for it to be missing from); it still force-sets
    ``timeout_seconds`` and refuses to spawn if an operator has explicitly
    denied PYTHONPATH via ``env_deny_names``.

    PATH is added after the allowlist call by the same convention every
    backend follows (``resolve_passthrough_env``'s own docstring: "PATH
    fallback is applied by each backend after calling this").
    """
    import reyn  # noqa: PLC0415
    from reyn.security.sandbox.policy import resolve_passthrough_env  # noqa: PLC0415

    tree = str(Path(reyn.__file__).resolve().parent.parent)  # dir containing the reyn pkg
    env = resolve_passthrough_env(policy)
    if "PATH" not in env and "PATH" in os.environ:
        env["PATH"] = os.environ["PATH"]
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
        # #1356 *preprocessor* harness, which is routed through
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
        actions: "dict[str, str] | None" = None,  # #1658 {identifier: action_name}
        sandbox_backend: Any = None,
        sandbox_policy: dict | None = None,
        allowed_modules: list[str] | None = None,
        timeout: float = 30.0,
        cwd: str | None = None,
        allow_unsandboxed: bool = False,
        cancel_event: "asyncio.Event | None" = None,
    ) -> dict[str, Any]:
        """Execute ``code`` in the CodeAct harness; service its tool() proxy via
        ``dispatch``. Returns the harness response dict
        (``{ok: True, result}`` | ``{ok: False, kind, error, traceback?}``), plus a
        ``status`` field (``ok`` | ``error`` | ``timeout`` | ``cancelled`` |
        ``sandbox_unavailable``) for the scheme layer.

        ``cancel_event`` (#4166, mirrors #1470's ``noop_backend``/``landlock``
        cancel-aware ``run()`` race): when provided, races the harness's
        completion against ``cancel_event.wait()`` the SAME way the sibling
        ``sandboxed_exec`` op's non-CodeAct subprocess launches already do —
        this runner did its own ``Popen`` instead of going through
        ``SandboxBackend.run()`` (see the module docstring for why: it needs a
        duplex control channel live during execution), so it never got the
        cancel-aware race the shared backend path has carried since #1470.
        ``cancel_event=None`` (the default) is byte-identical to before —
        only the wall-clock ``timeout`` bounds the run.

        **Fail-closed** (owner-signed): CodeAct runs ONLY under an available OS
        sandbox (Seatbelt / Landlock). When no real backend is available the run is
        refused (``sandbox_unavailable``), never silently downgraded to an
        unsandboxed subprocess. ``allow_unsandboxed=True`` is a **test-only** escape
        for exercising the transport/proxy core without a sandbox; production callers
        (the CodeAct scheme) never set it.

        The wrap (Seatbelt or Landlock, whichever the backend is) is resolved via
        ``sandbox_backend.wrap_command(...)`` — see ``_resolve_sandbox_spawn``.
        """
        base_argv = [self.python_executable, "-m", "reyn.core.kernel._codeact_harness"]
        argv, cleanup, spawn_error, resolved_policy = self._resolve_sandbox_spawn(
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
            # #1658: {identifier: action_name} — the harness injects a gated direct-
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
                env=_harness_subprocess_env(resolved_policy),  # #1609/#3822: allowlisted env + PYTHONPATH
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

        # Writes the request to stdin (the child reads it fully before touching the
        # control channel), then reads stdout/stderr + waits. It runs in an executor
        # thread, so the ``service_task`` services the control channel concurrently
        # on the event loop while the child blocks on a mid-execution tool() call.
        #
        # ``communicate_capped``, not ``proc.communicate`` (#3822): the same reader
        # every other command-level launch route already uses, capping each stream
        # and draining the excess so the child never blocks on a full pipe. Plain
        # ``communicate`` reads without a bound, and _subprocess_io's own docstring
        # says why that is not survivable — "emitting unbounded output can OOM the
        # host BEFORE the wall-clock timeout fires". The 30s timeout below is
        # therefore not a substitute for the cap; it is the thing the cap exists to
        # arrive ahead of. This seam is also the one running model-authored code, so
        # a snippet printing in a loop is an ordinary mistake rather than an exotic
        # one.
        request_bytes = json.dumps(request).encode("utf-8")
        # #4271/#4277: the inner communicate_capped timeout must be STRICTLY
        # LARGER than every outer asyncio.wait_for/asyncio.wait timeout that
        # also races this future — same value means "who reaches their own
        # deadline first" decides the outcome, not the outer deadline that
        # OWNS it. Passing the SAME `timeout` here let subprocess.TimeoutExpired
        # (raised inside the executor thread) escape through the no-cancel
        # branch's `except asyncio.TimeoutError` (a DIFFERENT exception type),
        # bypassing the status='timeout' envelope entirely (#4277 CI RED:
        # test_codeact_timeout_kill_no_attributeerror_without_killpg). The
        # inner value's only job is "never unbounded" — not "enforce the
        # deadline", which the outer wait_for/wait already owns.
        comm_future: asyncio.Future = loop.run_in_executor(
            None,
            lambda: communicate_capped(
                proc, input=request_bytes, timeout=timeout + POST_KILL_DRAIN_GRACE_SECONDS,
            ),
        )
        timed_out = False
        cancelled = False
        truncated = False
        # #4924: tied to actual EXECUTION of the kill call (set only on the
        # line immediately after `kill_process_tree(proc)` returns), never a
        # hardcoded literal in the return dict below — a literal would be
        # tautological with `cancelled` itself (true on every path that
        # reaches the `if cancelled:` return, including a future regression
        # that reaches it WITHOUT ever calling kill_process_tree at all),
        # closing zero of the gaps #4923's disclosed elapsed-time proxy was
        # standing in for. This variable is what makes `killed` in the
        # return dict a real signal instead of restating `status ==
        # "cancelled"` under a different name.
        killed = False
        # #4166: cancel_event=None is the byte-identical original path
        # (asyncio.wait_for against the wall-clock timeout alone). When
        # provided, race BOTH the timeout and the event — mirrors
        # noop_backend.py's cancel-aware run() (#1470) exactly, so a
        # cancel_inflight() during a running snippet kills the subprocess
        # instead of completing it out from under the (already-settled)
        # task, the gap #4166 measured live.
        try:
            if cancel_event is None:
                try:
                    stdout_b, stderr_b, truncated = await asyncio.wait_for(
                        comm_future, timeout=timeout
                    )
                except asyncio.TimeoutError:
                    timed_out = True
                    service_task.cancel()
                    await kill_process_tree(proc)
                    stdout_b, stderr_b = b"", b""
                else:
                    # Normal exit: the child sent op="final" then closed the channel
                    # (EOF), so DRAIN the service task (bounded) to populate
                    # final_box, rather than cancelling it mid-frame.
                    try:
                        await asyncio.wait_for(service_task, timeout=2.0)
                    except Exception:  # noqa: BLE001 — drain best-effort; cancel if it hangs
                        service_task.cancel()
            else:
                cancel_task = asyncio.create_task(cancel_event.wait())
                done, _ = await asyncio.wait(
                    {comm_future, cancel_task},
                    timeout=timeout,
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if cancel_task in done:
                    cancelled = True
                    service_task.cancel()
                    await kill_process_tree(proc)
                    killed = True
                    stdout_b, stderr_b = b"", b""
                elif not done:
                    timed_out = True
                    cancel_task.cancel()
                    service_task.cancel()
                    await kill_process_tree(proc)
                    stdout_b, stderr_b = b"", b""
                else:
                    cancel_task.cancel()
                    stdout_b, stderr_b, truncated = comm_future.result()
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

        if cancelled:
            # #4924 (architect ruling): a discriminated-union-safe seam for a
            # consumer that needs to know kill_process_tree was actually
            # invoked for this cancellation, not just that this call
            # returned — a real alternative to the elapsed-time proxy
            # #4923 was left disclosing. `killed` is ALWAYS present when
            # `status == "cancelled"` (never conditionally added only when
            # a kill "worked") — presence must never carry information in
            # an envelope that already has discriminators (`status`/
            # `kind`); `status == "cancelled"` is what a consumer branches
            # on, `killed` is data attached to that branch, not a second
            # discriminator.
            #
            # `killed: bool`, not `returncode: int` — kill_process_tree()
            # (the shared reaper _subprocess_io.py:284) has no return value
            # today, and its own docstring is explicit that graceful
            # (SIGTERM, reaped) vs. forced (SIGKILL after grace_seconds)
            # produce DIFFERENT returncodes; exposing a real returncode
            # here would need extending that SHARED helper's contract
            # (used by every sandbox backend, not just CodeAct) — out of
            # #4924's scope, which is "stop using a duration as this
            # test's proxy," not "add OS-level kill-signal fidelity."
            # `killed` reports the information this call site ALREADY has
            # (kill_process_tree was invoked for this cancellation) rather
            # than reaching for new information — if a real returncode
            # signal is needed later, `killed` can be replaced by it
            # without a second migration of THIS envelope's shape.
            return {
                "ok": False, "status": "cancelled",
                "kind": "Cancelled", "error": "codeact run was cancelled",
                "killed": killed,
            }
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
            # Surfaced, never silent (#3822). A cap that drops output without
            # saying so leaves the reader comparing a truncated stdout against
            # what they expected and concluding the snippet misbehaved — the
            # #3688 shape, where a region showed less than it held and the
            # absence was indistinguishable from the thing never existing.
            if truncated:
                final["truncated"] = True
            return final
        response = self._parse_response(stdout_b, stderr_b, proc.returncode)
        if truncated and isinstance(response, dict):
            response["truncated"] = True
        return response

    def _resolve_sandbox_spawn(
        self,
        base_argv: list[str],
        sandbox_backend: Any,
        sandbox_policy: dict | None,
        timeout: float,
        allow_unsandboxed: bool,
    ) -> tuple[list[str] | None, Callable[[], None] | None, str | None, "SandboxPolicy | None"]:
        """Resolve the spawn argv + a cleanup callable for the active sandbox, or an
        error string (fail-closed). Returns ``(argv, cleanup, error, policy)``;
        exactly one of ``argv`` / ``error`` is non-None. ``policy`` is the resolved
        ``SandboxPolicy`` whenever ``argv`` is non-None — the caller uses it to build
        the child's env through the SAME allowlist chokepoint (:func:`resolve_passthrough_env`)
        every other sandboxed launch route uses (#3822 env), instead of a bespoke
        ``dict(os.environ)`` copy.

        - Any AVAILABLE real backend (Seatbelt / Landlock): delegate to the
          backend's own ``wrap_command(base_argv, policy)`` (#2626's
          ``SandboxBackend`` abstraction) — the SAME wrap logic every other
          command-level launch route uses (single-abstraction, #2628). The
          inherited socketpair fd survives both backends' wraps (an AF_UNIX
          socketpair is not a ``network*`` socket).
        - noop / None / unavailable: fail-closed unless ``allow_unsandboxed`` (a
          test-only escape for the transport/proxy core). NoopBackend's
          ``wrap_command`` is a passthrough (no isolation), so it is deliberately
          excluded here — CodeAct must never silently run unsandboxed.

        ``PYTHONPATH`` reaches the harness subprocess without any forcing
        (#3901 PR-B ④: env is full-compat by default, owner ruling B — the
        #1609 multi-worktree fix this forced ``env_passthrough`` to solve no
        longer needs solving, since a compat default already passes
        PYTHONPATH through). If an operator EXPLICITLY denies ``PYTHONPATH``
        via ``env_deny_names``, this refuses to spawn rather than silently
        overriding that declared will — CodeAct's structural need for the
        name does not entitle it to win over an operator's own deny (#3901
        Q2: "a deny that loses to an allow is not a deny" cuts both ways).
        """
        from reyn.security.sandbox import SandboxPolicy  # noqa: PLC0415

        name = getattr(sandbox_backend, "name", None)
        available = bool(sandbox_backend is not None and sandbox_backend.available())

        if sandbox_backend is None or name in (None, "noop") or not available:
            if allow_unsandboxed:
                return base_argv, None, None, SandboxPolicy()
            return None, None, (
                "CodeAct requires an available OS sandbox backend (Seatbelt / "
                "Landlock); none available — refusing to run unsandboxed (fail-closed)."
            ), None

        policy_dict = dict(sandbox_policy or {})
        policy_dict["timeout_seconds"] = timeout
        # #3901 PR-B ④: PYTHONPATH passes by the env-compat default — no
        # forcing needed. But do not silently strip an operator's explicit
        # deny of it either; refuse loudly and name why (Q2).
        if "PYTHONPATH" in policy_dict.get("env_deny_names", []):
            return None, None, (
                "CodeAct cannot run: the sandbox policy denies PYTHONPATH, "
                "which the CodeAct subprocess needs to resolve the reyn "
                "tree. Remove it from env_deny_names, or disable CodeAct."
            ), None
        policy = SandboxPolicy(**policy_dict)

        try:
            wrapped = sandbox_backend.wrap_command(base_argv, policy)
        except Exception as exc:  # noqa: BLE001 — fail-closed on any wrap failure
            return None, None, f"CodeAct: sandbox_backend.wrap_command failed: {exc}", None

        return wrapped.argv, wrapped.cleanup, None, policy

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
