"""PhaseRouterLoopHost — #1092 PR-A (FD1, ADR-0036).

The phase-side ``RouterLoopCore`` implementation that lets a phase act-loop
drive the shared chat ``RouterLoop`` (Fork 1 convergence). It mirrors the
proven narrow ``_PlanStepHost`` (``chat/planner.py``) shape but is
TERMINAL-VALUED: a phase has no parent chat host to delegate to, and — per
#1212 PR3 decision A — no skills / agents / mcp / universal catalog.

It owns the catalog-source REPLACE seam:

- ``get_phase_op_catalog`` — the catalog-source REPLACE seam (``RouterLoop.run``
  uses this INSTEAD of chat-discovery ``build_tools`` when present).

The earlier op-execution seam (``execute_phase_op`` / ``RouterLoop._execute_tool``
host delegation, #1234 FD1 beta) was OBVIATED by the #1240 catalog axis: a phase's
op tool NAMES are now the unified fine registry kinds (``read_file`` … plus
``invoke_skill`` / ``call_mcp_tool``), so ``RouterLoop._invoke_router_tool`` routes
them through its existing ``REGISTRY_DISPATCH_TOOLS`` registry path — no
phase-specific exec hook needed (RouterLoop still holds no phase op-kind strings,
P7). The convergence wiring (PR-B) closes the two residuals this leaves:
(1) add ``edit_file`` / ``glob_files`` / ``grep_files`` to ``REGISTRY_DISPATCH_TOOLS``
(registry ToolDefs that chat never exposed as router tools), and
(2) implement :meth:`make_router_op_context` to return a phase ``OpContext``
(carrying the phase ``PermissionDecl`` / ``allowed_ops`` / sandbox policy) so the
registry handlers enforce phase permissions — the role the obviated seam's
``control_ir_executor`` dispatch played.

This host is the chat-vs-phase polymorphism point — the same role
``RouterHostAdapter`` (chat) and ``_PlanStepHost`` (plan-step) play for their loops.
It is INERT until ``PhaseExecutor`` wires it in (PR-B); today's phase act-loop still
runs the json-mode ``_run_op_loop`` unchanged.
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
        """Phase ``OpContext`` factory for the registry tool-dispatch handlers.

        RouterLoop's ``op_context_factory`` (= this method) feeds the registry
        tool-dispatch handlers (``REGISTRY_DISPATCH_TOOLS`` path). With the op-exec
        seam obviated (see module docstring), phase ops route through that same
        registry path, so this returns the SAME phase ``OpContext`` the json-mode
        op-loop builds — delegated to ``ControlIRExecutor._build_ctx`` with the
        phase ``PermissionDecl`` + sandbox policy so the registry handlers enforce
        phase permissions identically to ``control_ir_executor.execute`` (the role
        the obviated seam played). Single-sourced via the executor so there is no
        second permission/sandbox provisioning path to drift (P3/P5).
        """
        return self._control_ir_executor._build_ctx(
            self._decl,
            self._phase,
            default_sandbox_policy=self._default_sandbox_policy,
        )

    def op_dispatch_memo(self) -> dict | None:
        """Phase-mode op-dispatch WAL-memoization context (#1092 PR-C-2.5).

        ``RouterLoop._execute_tool`` consults this hook to decide whether a phase
        op dispatch is crash-resume memoized. A phase host returns the per-phase
        WAL wiring (``state_log`` + ``skill_run_id`` + ``resume_plan`` + ``phase``)
        so the dispatch threads them into ``dispatch_tool`` (with a phase-relative
        ``op_invocation_id``), reproducing the json-mode-equal crash-resume HARD
        GATE (#1225 Decision A): on resume the op memo-HITS and does not
        re-execute. Single-sourced from the shared ``ControlIRExecutor`` so there
        is no second resume-wiring path to drift (P5/P6).

        Chat hosts do NOT implement this method — ``RouterLoop._execute_tool``
        getattr-guards it to ``None``, leaving the chat dispatch path byte-identical
        (``caller_kind="router"``, no WAL step). Returns ``None`` here too when the
        executor has no WAL wired (state_log/skill_run_id absent = non-resumable run),
        so a non-resumable phase run also stays on the plain dispatch path.
        """
        cie = self._control_ir_executor
        state_log = getattr(cie, "_state_log", None)
        skill_run_id = getattr(cie, "_skill_run_id", None)
        if state_log is None or skill_run_id is None:
            return None
        return {
            "state_log": state_log,
            "skill_run_id": skill_run_id,
            "resume_plan": getattr(cie, "_resume_plan", None),
            "phase": self._phase,
        }

    # ── Chat-discovery methods (phase = empty) ────────────────────────────
    # #1092 PR-C-0: ``RouterLoop._build_router_caller_state`` calls these EAGERLY
    # while building the per-dispatch RouterCallerState (router_loop.py). A phase
    # has no skills/agents catalog (#1212 PR3 decision A — the catalog is the
    # phase op tools via ``get_phase_op_catalog``), so they return empty. The
    # eager call is also getattr-guarded on the RouterLoop side (defence in depth);
    # implementing them here makes the "phase has no chat discovery" contract
    # explicit rather than relying on the guard's default.

    def list_available_skills(self) -> list:
        return []

    def list_available_agents(self) -> list:
        return []

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
