"""EnumerateAllScheme — flat-native-JSON tool-use scheme (#1593 PR-2).

The simple, deterministic **baseline** scheme: present *every* usable tool flatly
in ``tools=`` (no universal category-wrapper discovery indirection) + a minimal
SP, and dispatch by name. Per the #1593 competitor research this is a fine
small-toolset baseline (max determinism, maps onto reyn's constrained
``candidate_outputs``) — it is **not** the weak-model fix (flat JSON is weakest
for weak models; CodeAct/PR-3 is the evidence-winner). Selected for the chat
layer via ``tool_use.scheme`` (FP-0066 P4b, #3247).

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

This module is the ``(enumerate-all, tool_calls)`` **cell**: it pairs the shared
``enumerate-all`` Exposure (``_enumerate_exposure``, which the ``content_fence``
cell also uses) with the ``tool_calls`` Encoder. What the two cells disagree
about is declared as an ``ExposureDeviation``, not written twice.

SP: the tool-use SP is owned by the scheme layer, not the OS — the exposure
carries the raw FACTS derived from ``layer_ctx`` and the encoder turns them into
the positional slot-map the ``Presentation`` hands over as ``tool_use_sp``. Key
difference from universal: ``search_actions_enabled = bool(search_visible)``
(NOT ``sv if univ else True`` — enumerate never has wrappers, so the
fallback-to-True branch does not apply).
"""
from __future__ import annotations

from reyn.tools.encoders import encoder_for_transport
from reyn.tools.scheme import (
    ExecContext,
    Execute,
    ExecutionResult,
    Interpretation,
    PlainText,
    Presentation,
    SchemeOps,
    register_scheme,
)
from reyn.tools.schemes._enumerate_exposure import (
    TOOL_CALLS_EXPOSURE_DEVIATION,
    build_enumerate_all_exposure,
)
from reyn.tools.transport import Transport


class EnumerateAllScheme:
    """Flat-native-JSON baseline tool-use scheme (#1593 PR-2)."""

    name = "enumerate-all"

    async def build_presentation(self, available, layer_ctx, ops: SchemeOps) -> Presentation:
        # Self-contained presentation (e2e-agreed seam, #1593): the exposure
        # composes what this cell shows — the prior-shape base tools + every
        # catalog action flat (no universal wrappers / no discovery), minus the
        # declared exclusions and minus the qualified spelling of anything the
        # base tools already name (#3428) — and the tool_calls encoder writes it
        # down. The
        # router holds host context + catalog, so the scheme stays P7-clean.
        # catalog_entries is async (the live-catalog enumeration awaits the
        # router caller-state / rag manifest); base_tools stays sync.
        # The builder's second return value is the composed dispatchable catalog,
        # which this cell does not need: on ``tool_calls`` the advertised payload
        # IS the dispatch gate's membership, so ``Presentation.dispatchable_catalog``
        # stays None and the OS keys the gate on ``tools=`` (router_loop's
        # "None ⇒ dispatch gate keys on self._catalog").
        exposure, _dispatchable = build_enumerate_all_exposure(
            catalog_entries=await ops.catalog_entries(),
            available=available,
            layer_ctx=layer_ctx,
            ops=ops,
            deviation=TOOL_CALLS_EXPOSURE_DEVIATION,
        )
        encoder = encoder_for_transport(Transport.TOOL_CALLS)
        return Presentation(
            tools_channel=encoder.encode_tools(exposure),
            tool_use_sp=encoder.encode_tool_use_sp(exposure),
        )

    def interpret(self, llm_response, *, tool_catalog: dict, ops: SchemeOps) -> Interpretation:
        # #1640: no tool_calls = a plain-text answer (the model's normal terminal) →
        # PlainText so the OS loop exits to the text-reply path. Without this guard,
        # resolve→[] → Execute([]) → the loop runs nothing → re-prompt → empty-content
        # turn → never terminates → 120s timeout (weak-model robustness bug). Mirrors
        # universal_category + retrieval (and root-3 #2's codeact no-fence→PlainText).
        if not getattr(llm_response, "tool_calls", None):
            return PlainText()
        # Flat (qualified) names resolve through the shared resolution (dedupe +
        # salvage/unwrap → effective names) so the OS exclude-gates pre-dispatch.
        actions = ops.resolve(llm_response, tool_catalog)
        return Execute(actions=actions)

    async def execute(self, interp: Interpretation, exec_ctx: ExecContext, ops: SchemeOps) -> ExecutionResult:
        assert isinstance(interp, Execute), "enumerate-all emits only Execute"
        # #4691 Phase B ①(remainder): forward the round's call_id — see
        # universal_category.py's own execute() for the full reasoning.
        results = await ops.dispatch(
            interp.actions, call_id=(getattr(exec_ctx, "extra", None) or {}).get("call_id"),
        )
        return ExecutionResult(tool_results=results)

    def format_feedback(self, result: ExecutionResult, ops: SchemeOps) -> list[dict]:
        # #1608: delegate to the OS substrate (now returns appendable messages);
        # enumerate-all's Execute feedback is identical to universal's.
        return ops.feedback(result)


__all__ = ["EnumerateAllScheme"]

# #1608: self-register on import (P7 — the OS resolve no longer names this class).
register_scheme(EnumerateAllScheme())
