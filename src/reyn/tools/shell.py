"""``shell`` ToolDefinition — pipeline DSL ``shell`` step sugar (#2593).

The pipeline DSL's ``shell = {command, timeout?, schema?, output?}`` step
(``reyn.core.pipeline.parser._parse_shell_step``) is tool-step SUGAR: it
compiles to a ``ToolStep(name="shell", args={"command": ..., "stdin_pipe":
ExprRef("pipe"), ["timeout": ...]})``. This module is that tool's
``ToolDefinition`` + handler.

Design (locked, #2593):
- ``command`` is STATIC (a literal or ``!expr``-resolved value at parse/step
  time — never re-templated here).
- the PREVIOUS step's pipe-data is threaded to the process's STDIN, JSON
  -encoded (so a structured pipe value survives the byte boundary).
- STDOUT becomes the step's return value (= next step's pipe-data / this
  step's ``output`` store) — JSON-decoded when it parses (so ``verify:
  schema``, which requires a ``dict``, can apply to a JSON-emitting shell
  command), else the raw text — optionally ``verify: schema``-checked by the
  executor's existing ``ToolStep`` schema-check (unchanged).

This is thin sugar over the EXISTING ``sandboxed_exec`` op — it does not
reinvent subprocess handling: it builds a ``SandboxedExecIROp`` running
``/bin/sh -c <command>`` with ``stdin`` set to the JSON-encoded pipe data,
and delegates to ``reyn.core.op_runtime.sandboxed_exec.handle`` via the SAME
``ToolContext`` → ``OpContext`` bridge ``reyn.tools.sandboxed_exec._handle``
uses (:func:`reyn.tools.sandboxed_exec.op_context_from_tool_context`) — same
sandbox confinement, same policy precedence, same audit events.
"""
from __future__ import annotations

import json
from typing import Any, Mapping

from reyn.tools.types import ToolContext, ToolDefinition, ToolGates

_SHELL_DESCRIPTION = (
    "Run a shell command (via sandboxed_exec) whose STDIN receives the "
    "previous pipeline step's pipe-data JSON-encoded, and whose STDOUT "
    "becomes this step's output. command: the shell command line "
    "(argv[0]='/bin/sh', argv[1]='-c'). timeout: wall-clock time limit in "
    "seconds (default 60). The sandbox policy (network access + filesystem "
    "scope) is the OPERATOR's, resolved by the OS — it is not chosen here."
)

_SHELL_PARAMETERS: dict[str, Any] = {
    "type": "object",
    "properties": {
        "command": {
            "type": "string",
            "description": "Shell command line, run as `/bin/sh -c <command>`.",
        },
        "stdin_pipe": {
            "description": "The previous pipeline step's pipe-data (JSON-encoded onto stdin).",
        },
        "timeout": {
            "type": "integer",
            "description": "Wall-clock time limit in seconds (default 60).",
        },
    },
    "required": ["command"],
}


async def _handle(args: Mapping[str, Any], ctx: ToolContext) -> Any:
    """Build a ``SandboxedExecIROp`` for ``/bin/sh -c <command>`` with the
    resolved ``stdin_pipe`` JSON-encoded onto stdin, and delegate to the
    EXISTING ``op_runtime.sandboxed_exec.handle`` (no new subprocess handling).

    Returns ``result["stdout"]`` (#2593 locked design) — JSON-decoded when it
    parses, else the raw text. The decode is required for ``verify: schema``
    to ever apply to a shell step's output at all: the executor's schema
    validator (``reyn.core.pipeline.schema.validate``) requires a ``dict``
    value (non-dict → an immediate ``type_mismatch``, by construction — see
    its top-of-function ``isinstance(value, dict)`` guard), and a subprocess's
    STDOUT is always raw text. A plain-text command's output still threads
    through unparsed (JSON-decode failure falls back to the text verbatim),
    so this is additive, not a behavior change for non-JSON commands.
    """
    from reyn.core.op_runtime.sandboxed_exec import handle as handle_sandboxed_exec
    from reyn.schemas.models import SandboxedExecIROp
    from reyn.tools.sandboxed_exec import op_context_from_tool_context

    stdin_bytes = json.dumps(args.get("stdin_pipe")).encode("utf-8")
    op = SandboxedExecIROp(
        kind="sandboxed_exec",
        argv=["/bin/sh", "-c", args["command"]],
        timeout_seconds=int(args.get("timeout", 60)),
        stdin=stdin_bytes,
    )
    legacy_ctx = await op_context_from_tool_context(ctx)
    result = await handle_sandboxed_exec(op=op, ctx=legacy_ctx)
    stdout = result["stdout"]
    try:
        return json.loads(stdout)
    except (json.JSONDecodeError, TypeError):
        return stdout


SHELL = ToolDefinition(
    name="shell",
    description=_SHELL_DESCRIPTION,
    parameters=_SHELL_PARAMETERS,
    gates=ToolGates(router="allow", phase="allow"),
    handler=_handle,
    category="execution",
    purity="side_effect",
)
