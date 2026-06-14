"""Child-process entry point for a CodeAct snippet (#1593 PR-3).

Invoked as ``python -m reyn.kernel._codeact_harness`` inside the sandbox. Unlike
``_python_harness`` (a pure one-shot function runner), CodeAct code interleaves
computation with **synchronous tool calls** mid-execution, so this harness opens a
**duplex control channel** to the parent: the only world-effect path exposed to the
snippet is a ``tool(name, **args)`` shim that round-trips each call to the parent,
where the SAME OS exclude + ``dispatch_tool`` + permission gate runs it (P5). The
snippet never holds permission authority or reaches Reyn internals directly.

Wire format (newline-delimited JSON over the inherited control fd)
------------------------------------------------------------------
Request (stdin, single JSON object):
    {
      "code":            "<the snippet>",
      "control_fd":      <int>,                # inherited AF_UNIX socketpair fd
      "allowed_modules": ["json", ...]         # safe-mode import allowlist
    }

Control channel (duplex, during execution):
    child → parent:  {"op": "tool_call", "name": "<qualified>", "args": {...}}\\n
    parent → child:  {"op": "result", "result": {...}}\\n     # dispatch_tool envelope

Response (stdout, single JSON object):
    {"ok": true,  "result": <JSON>}                          # snippet completed
    {"ok": false, "error": "<message>", "kind": "<class>"}   # snippet raised

The control fd survives the OS sandbox (an inherited AF_UNIX socketpair is not a
``network*`` socket — verified on Seatbelt under ``(deny default)+(deny network*)``);
it is the single, audited hole, carrying only marshalled tool calls the parent
re-gates. Direct FS-write / network / subprocess remain blocked by the sandbox.
"""
from __future__ import annotations

import ast
import contextlib
import io
import json
import socket
import sys
import traceback
from typing import Any

# Reuse the existing safe-mode validator + restricted builtins (#1593
# correspondence: harness restricted-namespace mechanism reused; CodeAct only
# adds the permission-proxy shim on top).
from reyn.kernel._python_harness import (
    _build_restricted_builtins,
    _validate_safe_ast,
)


class _ControlChannel:
    """Newline-delimited JSON duplex channel to the parent over the inherited fd."""

    def __init__(self, sock: socket.socket) -> None:
        self._sock = sock
        self._buf = b""

    def call(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Send one request and block for the single-line reply."""
        self._sock.sendall((json.dumps(payload) + "\n").encode("utf-8"))
        return self._recv_line()

    def _recv_line(self) -> dict[str, Any]:
        while b"\n" not in self._buf:
            chunk = self._sock.recv(65536)
            if not chunk:
                raise _ControlChannelClosed(
                    "parent closed the CodeAct control channel mid-call"
                )
            self._buf += chunk
        line, _, self._buf = self._buf.partition(b"\n")
        return json.loads(line.decode("utf-8"))


class _ControlChannelClosed(RuntimeError):
    """The parent hung up the control channel before replying."""


def _make_tool_shim(channel: _ControlChannel):
    """Build the ``tool(name, **args)`` callable injected into the snippet namespace.

    Marshals ``(name, args)`` to the parent, which runs the OS exclude +
    ``dispatch_tool`` + permission gate, and returns the result envelope's payload.
    A ``status == "error"`` envelope is raised as ``ToolError`` so the snippet can
    try/except it the way it would a normal Python call (permission_denied /
    tool_excluded / unknown_tool surface here)."""

    def tool(name: str, **args: Any) -> Any:
        reply = channel.call({"op": "tool_call", "name": name, "args": args})
        result = reply.get("result", {})
        if isinstance(result, dict) and result.get("status") == "error":
            err = result.get("error", {}) or {}
            raise ToolError(
                err.get("message", "tool call failed"),
                kind=err.get("kind", "error"),
                name=name,
            )
        # dispatch_tool success envelope is {"status": "ok", "data": <value>}.
        if isinstance(result, dict) and "data" in result:
            return result["data"]
        return result

    return tool


class ToolError(RuntimeError):
    """Raised inside a CodeAct snippet when a proxied tool call returns an error
    envelope (permission_denied / tool_excluded / unknown_tool / exception)."""

    def __init__(self, message: str, *, kind: str = "error", name: str = "") -> None:
        super().__init__(message)
        self.kind = kind
        self.name = name


def _exec_codeact(
    code: str, channel: _ControlChannel, allowed_modules: frozenset[str],
) -> tuple[Any, str]:
    """Validate + exec the snippet with the restricted builtins + the tool shim.

    Returns ``(result, captured_stdout)`` — the snippet's ``result`` binding (or
    None) and anything it wrote to stdout. The snippet's stdout is captured into a
    buffer, NOT left on the process stdout: the harness uses stdout for its own JSON
    result envelope, so an un-captured ``print(...)`` corrupts that envelope (the
    parent's ``json.loads`` then fails = MalformedResponse, #1593 live-verify). The
    snippet affects the world ONLY through ``tool(...)`` (the parent-gated proxy);
    the restricted builtins block ``open`` / ``eval`` / ``__import__`` of
    non-allowlisted modules (defense-in-depth on top of the sandbox)."""
    tree = ast.parse(code, filename="<codeact>")
    _validate_safe_ast(tree, allowed_modules)
    builtins_dict = _build_restricted_builtins(allowed_modules)

    namespace: dict[str, Any] = {
        "__builtins__": builtins_dict,
        "__name__": "__reyn_codeact__",
        "tool": _make_tool_shim(channel),
    }
    compiled = compile(tree, filename="<codeact>", mode="exec")
    stdout_buf = io.StringIO()
    with contextlib.redirect_stdout(stdout_buf):
        exec(compiled, namespace)  # noqa: S102 — sandboxed subprocess + restricted ns
    return namespace.get("result"), stdout_buf.getvalue()


def _read_request() -> dict[str, Any]:
    raw = sys.stdin.read()
    if not raw:
        raise ValueError("codeact harness received empty stdin")
    return json.loads(raw)


def _write_response(payload: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(payload, ensure_ascii=False, default=str))
    sys.stdout.flush()


def main() -> int:
    sock: socket.socket | None = None
    try:
        req = _read_request()
        code = str(req["code"])
        control_fd = int(req["control_fd"])
        allowed_modules = frozenset(req.get("allowed_modules") or [])

        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM, fileno=control_fd)
        channel = _ControlChannel(sock)

        result, captured_stdout = _exec_codeact(code, channel, allowed_modules)
        # When the snippet printed instead of assigning ``result`` (a common weak
        # -model idiom — ``print(tool(...))``), surface the captured stdout as the
        # result so the turn still yields a usable observation (#1593 live-verify).
        if result is None and captured_stdout.strip():
            result = captured_stdout.strip()
        _write_response({"ok": True, "result": result})
        return 0
    except Exception as exc:  # noqa: BLE001 — surface every failure as a response
        _write_response({
            "ok": False,
            "kind": type(exc).__name__,
            "error": str(exc),
            "traceback": traceback.format_exc(limit=20),
        })
        return 1
    finally:
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass


if __name__ == "__main__":
    sys.exit(main())
