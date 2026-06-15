"""EnumerateAllScheme — flat-native-JSON tool-use scheme (#1593 PR-2).

The simple, deterministic **baseline** scheme: present *every* usable tool flatly
in ``tools=`` (no universal category-wrapper discovery indirection) + a minimal
SP, and dispatch by name. Per the #1593 competitor research this is a fine
small-toolset baseline (max determinism, maps onto reyn's constrained
``candidate_outputs``) — it is **not** the weak-model fix (flat JSON is weakest
for weak models; CodeAct/PR-3 is the evidence-winner). Selected per-layer via
``tool_use: {chat/step/phase}``.

Unlike ``UniversalCategoryScheme`` (which delegates *all four* methods to the
router ``SchemeOps``, byte-identical), enumerate-all is the first **self-contained**
scheme: its presentation differs (flat catalog enumeration vs the 4 wrappers), so
``build_presentation`` is genuinely its own. The other three reuse the shared
substrate:

- ``interpret``       → ``ops.resolve`` (the names are qualified ``<category>__<entry>``
  so the existing resolution/dedupe → effective names works unchanged) → ``Execute``.
- ``execute``         → ``ops.dispatch`` (the pure-OS ``dispatch_tool`` / permission
  substrate, P5 — identical to universal).
- ``format_feedback`` → ``ops.feedback`` (the basic tool_result formatting, a JSON-
  scheme shared base — confirmed reuse, lead #1593).

SP: #1627 Stage 2: ``build_presentation`` now owns its tool-use SP via the slot-map.
It calls ``build_universal_tool_use_slots`` with enumerate's 5 inputs derived from
``layer_ctx`` (the OS-supplied raw FACTS), and attaches the resulting slot-map to
the returned ``Presentation`` as ``tool_use_sp``. This relocates the tier→discovery-
mandate POLICY out of the OS and into the scheme layer (CHAR-IDENTICAL: Stage 0
proved the two paths produce the same SP bytes). Key difference from universal:
``search_actions_enabled = bool(search_visible)`` (NOT ``sv if univ else True`` —
enumerate never has wrappers, so the fallback-to-True branch does not apply).
``sp_params`` is kept AS-IS (Stage 4 removes it; harmless now).
"""
from __future__ import annotations

from reyn.tools.scheme import (
    ExecContext,
    Execute,
    ExecutionResult,
    Interpretation,
    Presentation,
    SchemeOps,
    register_scheme,
)
from reyn.tools.schemes._discovery import tier_wants_discovery_mandate
from reyn.tools.schemes._universal_sp import build_universal_tool_use_slots


class EnumerateAllScheme:
    """Flat-native-JSON baseline tool-use scheme (#1593 PR-2)."""

    name = "enumerate-all"

    async def build_presentation(self, available, layer_ctx, ops: SchemeOps) -> Presentation:
        # Self-contained presentation (e2e-agreed seam, #1593): compose the flat
        # tools= from the router's building-block ops — the prior-shape base tools
        # + every catalog action flat (no universal wrappers / no discovery). The
        # router holds host context + catalog, so the scheme stays P7-clean.
        # catalog_entries is async (the live-catalog enumeration awaits the
        # router caller-state / rag manifest); base_tools stays sync.
        flat_tools = list(ops.base_tools(available, layer_ctx)) + list(await ops.catalog_entries())
        # #1627 Stage 2: own the tool-use SP via the slot-map.
        # Derive the 5 builder inputs from the raw FACTS in layer_ctx. Enumerate
        # NEVER has universal wrappers, so universal_wrappers_enabled is always
        # False. CRITICAL: search_actions_enabled = bool(search_visible) directly
        # (NOT the universal formula ``sv if univ else True`` — that fallback-to-True
        # branch only applies when universal wrappers are off; enumerate is
        # always-off so the direct bool(search_visible) is the correct mapping).
        slots = build_universal_tool_use_slots(
            universal_wrappers_enabled=False,
            search_actions_enabled=bool(layer_ctx.get("search_visible", False)),
            discovery_mandate=tier_wants_discovery_mandate(layer_ctx.get("router_model")),
            has_hot_list_aliases=bool((available or {}).get("hot_list_aliases")),
            non_interactive=bool(layer_ctx.get("non_interactive", False)),
        )
        # #1627 Stage 4: sp_params removed — build_system_prompt no longer reads it.
        return Presentation(llm_tools_payload=flat_tools, tool_use_sp=slots)

    def interpret(self, llm_response, *, tool_catalog: dict, ops: SchemeOps) -> Interpretation:
        # Flat (qualified) names resolve through the shared resolution (dedupe +
        # salvage/unwrap → effective names) so the OS exclude-gates pre-dispatch.
        actions = ops.resolve(llm_response, tool_catalog)
        return Execute(actions=actions)

    async def execute(self, interp: Interpretation, exec_ctx: ExecContext, ops: SchemeOps) -> ExecutionResult:
        assert isinstance(interp, Execute), "enumerate-all emits only Execute"
        results = await ops.dispatch(interp.actions)
        return ExecutionResult(tool_results=results)

    def format_feedback(self, result: ExecutionResult, ops: SchemeOps) -> list[dict]:
        # #1608: delegate to the OS substrate (now returns appendable messages);
        # enumerate-all's Execute feedback is identical to universal's.
        return ops.feedback(result)


__all__ = ["EnumerateAllScheme"]

# #1608: self-register on import (P7 — the OS resolve no longer names this class).
register_scheme(EnumerateAllScheme())
