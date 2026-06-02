"""PhaseRouterLoopHost — #1092 PR-A (FD1, ADR-0036).

The phase-side ``RouterLoopCore`` implementation that lets a phase act-loop
drive the shared chat ``RouterLoop`` (Fork 1 convergence). It mirrors the
proven narrow ``_PlanStepHost`` (``chat/planner.py``) shape but is
TERMINAL-VALUED: a phase has no parent chat host to delegate to, and — per
#1212 PR3 decision A — no skills / agents / mcp / universal catalog.

It owns BOTH halves of the convergence seam:

- ``get_phase_op_catalog`` — the catalog-source REPLACE seam (``RouterLoop.run``
  uses this INSTEAD of chat-discovery ``build_tools`` when present).
- ``execute_phase_op`` — the op-execution seam (``RouterLoop._execute_tool``
  delegates every phase tool call here). It converts the native tool call to a
  ``ControlIROp`` and runs it through the SHARED ``control_ir_executor`` so
  dispatch / permission / events / WAL match the json-mode op-loop exactly.

RouterLoop stays generic (P7): it holds no phase op-kind strings. This host is
the chat-vs-phase polymorphism point — the same role ``RouterHostAdapter``
(chat) and ``_PlanStepHost`` (plan-step) play for their loops.
"""

from __future__ import annotations

from typing import Any, Callable


class PhaseRouterLoopHost:
    """RouterLoopCore for a single phase act-loop iteration set.

    Construction deps all originate in ``PhaseExecutor._run_op_loop``'s scope
    (``phase_executor.py``): the shared ``control_ir_executor``, the phase
    ``EventLog``, the current phase name + ``PermissionDecl`` + ``allowed_ops``
    + phase-default ``SandboxPolicy``, plus agent identity and the OS model
    resolver (passed as ``resolve_model_fn`` so this host stays decoupled from
    OSRuntime's resolver wiring — the loop only needs ``name -> model id``).
    """

    def __init__(
        self,
        *,
        control_ir_executor: Any,
        events: Any,
        phase: str,
        decl: Any,
        allowed_ops: set[str] | None,
        default_sandbox_policy: dict | None,
        agent_name: str,
        agent_role: str,
        output_language: str | None,
        resolve_model_fn: Callable[[str], str],
    ) -> None:
        self._control_ir_executor = control_ir_executor
        self._events = events
        self._phase = phase
        self._decl = decl
        self._allowed_ops = allowed_ops
        self._default_sandbox_policy = default_sandbox_policy
        self._agent_name = agent_name
        self._agent_role = agent_role
        self._output_language = output_language
        self._resolve_model_fn = resolve_model_fn

    # ── RouterLoopCore identity / static config ───────────────────────────

    @property
    def agent_name(self) -> str:
        return self._agent_name

    @property
    def agent_role(self) -> str:
        return self._agent_role

    @property
    def output_language(self) -> str | None:
        return self._output_language

    @property
    def events(self) -> Any:
        return self._events

    def resolve_model(self, name: str) -> str:
        return self._resolve_model_fn(name)

    def make_router_op_context(self) -> Any:
        """Vestigial for phase — returns None.

        RouterLoop's ``op_context_factory`` (= this method) feeds its CHAT
        tool-dispatch handlers (file / web / mcp), which phase ops BYPASS: a
        phase op routes via :meth:`execute_phase_op` → ``control_ir_executor``,
        which builds its OWN ``OpContext`` internally. So this is never reached
        on the phase path; it exists only to satisfy ``RouterLoopCore``.
        """
        return None

    async def put_outbox(self, *, kind: str, text: str, meta: dict) -> None:
        """Phase NO-OP — a concept-absent legitimate no-op (P-clean).

        A phase's output is its artifact + transition, not a user-facing outbox
        stream. The phase act-loop accumulates op results into the RouterLoop
        message history (the phase's working state), so no-op-ing the outbox
        drops nothing the phase relies on — unlike a fragile chat stub.
        """
        return None

    # ── Catalog-source REPLACE seam ───────────────────────────────────────

    def get_phase_op_catalog(self) -> list[dict]:
        """The phase's op tool catalog in litellm ``tools=`` list shape.

        REPLACES chat-discovery in ``RouterLoop.run`` (a phase has no skills /
        agents / mcp / universal). Mirrors the exact build the json-mode
        ``_run_op_loop`` does today (``phase_executor.py``): ``allowed_ops`` →
        ``_build_phase_tool_catalog`` → ``{"type": "function", **entry}`` list.
        """
        from reyn.kernel.control_ir_executor import _build_phase_tool_catalog

        catalog = _build_phase_tool_catalog(self._allowed_ops or set())
        return [{"type": "function", **entry} for entry in catalog.values()]

    # ── op-execution seam ─────────────────────────────────────────────────

    async def execute_phase_op(self, *, name: str, args: dict) -> Any:
        """Run one phase op through the SHARED control_ir_executor.

        Converts the native tool call to a ``ControlIROp`` then dispatches via
        ``control_ir_executor.execute`` — the same dispatch / permission /
        events / WAL path the json-mode op-loop uses (``_run_op_loop`` @
        ``execute(ops, ...)``). On an invalid op shape it MIRRORS the json-mode
        per-turn validation (emit ``validation_error`` + return an error result
        the model sees in the next turn's message history) rather than crashing
        the loop.
        """
        from reyn.kernel.op_loop import tool_call_to_control_ir_op

        try:
            op = tool_call_to_control_ir_op(
                {"function": {"name": name, "arguments": args}}
            )
        except Exception as exc:  # noqa: BLE001 — invalid op shape (mirror json-mode)
            self._events.emit(
                "validation_error",
                phase=self._phase,
                error=f"invalid tool_call op: {exc}",
            )
            return {"status": "error", "kind": "invalid_op", "error": str(exc)}

        results = await self._control_ir_executor.execute(
            [op],
            phase=self._phase,
            decl=self._decl,
            allowed_ops=self._allowed_ops,
            default_sandbox_policy=self._default_sandbox_policy,
        )
        return results[0] if results else {}
