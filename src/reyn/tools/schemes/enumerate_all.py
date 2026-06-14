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

SP: returns ``sp_params{"universal_wrappers_enabled": False}`` → the router's
``build_system_prompt`` emits the prior-shape (no wrapper-chain) tool-use SP. No
``build_system_prompt`` surgery (the fragment-extraction the earlier plan floated
is unnecessary — the existing gate yields the minimal SP), so PR-2 stays under
``schemes/`` + config and does not collide with PR-3's parallel SP work.
"""
from __future__ import annotations

from reyn.tools.scheme import (
    ExecContext,
    Execute,
    ExecutionResult,
    Interpretation,
    Presentation,
    SchemeOps,
)


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
        # Prior-shape (no wrapper-chain) SP — the existing gate yields the minimal
        # tool-use instructions enumerate-all wants; no build_system_prompt change
        # (the fragment-extraction the earlier plan floated is unnecessary).
        sp_params = {
            "universal_wrappers_enabled": False,
            "search_actions_enabled": bool(layer_ctx.get("search_visible", False)),
        }
        return Presentation(llm_tools_payload=flat_tools, sp_params=sp_params)

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
