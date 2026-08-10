"""run_prompt ToolDefinition — proposal 0067 P4d/P4e (#3978).

Router-only (gates.router=allow).

collect="attached" | "async"
-----------------------------
The proposal names two ``collect`` values: ``"attached"`` (waits inline,
returns the reply) and ``"async"`` (returns a task_id immediately, result
arrives later via ``task_settled``). Both are implemented here.

Synchronous run+collect semantics (collect="attached")
---------------------------------------------------------
Unlike ``delegate_to_agent`` (async-dispatch: ends the turn, the reply
arrives in a FUTURE RouterLoop invocation), ``run_prompt(collect="attached")``
drives the target peer INLINE and returns the reply in THIS same tool call
— the same posture ``run_agent_step``/``run_pipeline_attached`` use, but
addressing an EXISTING live peer rather than a session this call spawns.
See ``session_api.run_prompt_result``'s docstring for the full
double-pump-refusal / lock / deadlock-timeout rationale — this file is thin
wiring only, mirroring ``send_to_session.py``.

dispatch_kind="sync": the tool call returns immediately once the reply (or
a typed refusal) is in hand — never ends the turn early the way
``delegate_to_agent``'s "async" posture does.

Async-dispatch semantics (collect="async")
----------------------------------------------
Proposal 0067 P4e (#3978), architect ruling 2026-08-10 (three rounds:
reply-routing identity, the register-per-call structural condition, this
function's own shape). ``run_prompt(collect="async")`` dispatches
``prompt`` to a LIVE peer as an ``agent_request`` and returns
``{"status": "started", "data": {"task_id": <chain_id>}}`` immediately —
it does NOT drive the target inline the way ``collect="attached"`` does,
so it needs none of that path's busy-check/lock/deadlock-timeout
machinery. The reply arrives later as a ``task_settled`` push, once the
peer's own turn responds. See ``session_api.run_prompt_async``'s
docstring for the full producer-identity / register-per-call rationale —
this file is thin wiring only, mirroring the ``collect="attached"``
branch above."""
from __future__ import annotations

from typing import Any, Mapping

from reyn.tools.descriptions import delegation as _delegation_descriptions
from reyn.tools.types import ToolContext, ToolDefinition, ToolGates, ToolResult

_RUN_PROMPT_DESCRIPTION = _delegation_descriptions.run_prompt.text

_RUN_PROMPT_PARAMETERS: dict[str, Any] = {
    "type": "object",
    "properties": {
        "agent": {
            "type": "string",
            "description": _delegation_descriptions.PARAMS["run_prompt"]["agent"].text,
        },
        "session": {
            "type": "string",
            "description": _delegation_descriptions.PARAMS["run_prompt"]["session"].text,
        },
        "prompt": {
            "type": "string",
            "description": _delegation_descriptions.PARAMS["run_prompt"]["prompt"].text,
        },
        "collect": {
            "type": "string",
            "enum": ["attached", "async"],
            "description": _delegation_descriptions.PARAMS["run_prompt"]["collect"].text,
        },
    },
    "required": ["agent", "session", "prompt", "collect"],
}


async def _handle(args: Mapping[str, Any], ctx: ToolContext) -> ToolResult:
    """Dispatch on ``collect`` to RouterCallerState.run_prompt_result_fn
    (``"attached"``) or .run_prompt_async_fn (``"async"``).

    Raises RuntimeError when router_state or the selected fn is missing
    (= mis-wiring; matches send_to_session's handler convention) — a host
    that genuinely doesn't support multi-session delivery leaves both
    None, and the tool should not even be catalog-visible on such a host.

    ``collect`` is validated by the JSON schema (enum=["attached", "async"])
    before this handler ever runs — no other value reaches this branch."""
    rs = ctx.router_state
    if args["collect"] == "async":
        if rs is None or rs.run_prompt_async_fn is None:
            raise RuntimeError(
                "run_prompt(collect=\"async\") handler requires "
                "ctx.router_state.run_prompt_async_fn to be populated by "
                "the dispatcher (= RouterLoop)."
            )
        return await rs.run_prompt_async_fn(
            agent=args["agent"], session=args["session"], prompt=args["prompt"],
        )
    if rs is None or rs.run_prompt_result_fn is None:
        raise RuntimeError(
            "run_prompt handler requires ctx.router_state.run_prompt_result_fn "
            "to be populated by the dispatcher (= RouterLoop)."
        )
    return await rs.run_prompt_result_fn(
        agent=args["agent"], session=args["session"], prompt=args["prompt"],
    )


from reyn.core.offload.canonical import run_prompt_to_canonical  # noqa: E402

RUN_PROMPT = ToolDefinition(
    canonical=run_prompt_to_canonical,
    name="run_prompt",
    router_dispatched=True,
    description=_RUN_PROMPT_DESCRIPTION,
    parameters=_RUN_PROMPT_PARAMETERS,
    gates=ToolGates(router="allow"),
    handler=_handle,
    category="delegation",
    purity="side_effect",
    dispatch_kind="sync",  # returns in-band once the reply/refusal is in hand
    returns_external_content=True,  # FP-0050/#1822: the PEER SESSION's own reply text, returned
    # synchronously as this tool's own result — see the classification comment in
    # tests/tools/test_returns_external_content_flagset_1822.py's _EXTERNAL set.
)
