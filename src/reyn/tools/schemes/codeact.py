"""CodeActScheme — the (enumerate-all, content_fence) transport-cell
implementation (#1593 PR-3; FP-0066 P4c, #3247).

Unlike universal-category (which delegates to the router's existing JSON tool
logic), CodeAct implements its own scheme logic: the LLM writes a Python snippet
and tool calls happen as **in-code calls**, each round-tripping through the
sandboxed ``CodeActRunner`` to the OS per-call gate (exclude + ``dispatch_tool``
+ permission, P5). A CodeAct call is therefore gated **>=** a JSON call (same gate
+ sandbox containment).

FP-0066 P4c clean-break: this class is no longer registered under the bare
name ``"codeact"`` (which read as if it were a 4th sibling scheme alongside
``category`` / ``enumerate-all`` / ``retrieval``). It self-registers under
``reyn.tools.transport.CONTENT_FENCE_ENUMERATE_ALL_SCHEME_NAME`` — reachable
ONLY by resolving ``(scheme="enumerate-all", transport=Transport.CONTENT_FENCE)``
through the P4a valid-pair registry (``tool_use.scheme: codeact`` was never a
valid config value; this closes the matching ``_SCHEMES``-level naming gap).

What is left in this module is the CELL, and only the cell: the
``enumerate-all`` exposure (``reyn.tools.schemes._enumerate_exposure``) decides
what is shown, the ``content_fence`` encoder (``reyn.tools.encoders``) renders
the code-API and owns the identifier map, and the transport's own behaviour —
fence extraction, sandboxed execution, the observation turn — is
``_content_fence_cell.ContentFenceCellScheme``, shared with every other cell on
this transport (#3376 P2). Nothing here is CodeAct-versus-another-cell except
the exposure.

The 4 ToolUseScheme methods (3 of them inherited from the transport base):
  - ``build_presentation`` → render the permission-eligible actions as a *code-API*
    (function signatures from ``ops.catalog_entries()``, excluded omitted).
  - ``interpret`` → extract the ``CodeBlock`` from the LLM response.
  - ``execute`` → run the snippet via ``CodeActRunner`` with the OS-provided per-call
    gate (``exec_ctx``) under ``exec_ctx.sandbox`` (fail-closed).
  - ``format_feedback`` → the runner result envelope back to the loop.

The OS gate + sandbox are provided via ``ExecContext`` (the OS assembles them in the
router's CodeBlock arm); the scheme never assembles a DispatchContext or reaches
permission internals — it orchestrates, the OS gates (P3/P7).
"""
from __future__ import annotations

from typing import Any

from reyn.tools.exposure import Exposure
from reyn.tools.scheme import register_scheme
from reyn.tools.schemes._content_fence_cell import ContentFenceCellScheme
from reyn.tools.schemes._enumerate_exposure import (
    CONTENT_FENCE_EXPOSURE_DEVIATION,
    build_enumerate_all_exposure,
)
from reyn.tools.transport import CONTENT_FENCE_ENUMERATE_ALL_SCHEME_NAME


class CodeActScheme(ContentFenceCellScheme):
    """CodeAct scheme (#1593 PR-3) — the ``enumerate-all`` presentation over the
    ``content_fence`` transport.

    ``name`` is the P4c-relocated ``_SCHEMES`` key (see module docstring) —
    not the literal ``"codeact"`` string; that name no longer exists in the
    registry."""

    name: str = CONTENT_FENCE_ENUMERATE_ALL_SCHEME_NAME

    async def build_exposure(
        self, available: Any, layer_ctx: Any, ops: Any,
    ) -> "tuple[Exposure, list[dict]]":
        """The ``enumerate-all`` exposure, over the catalog alone.

        ``ops.catalog_entries()`` is async (the SchemeOps adapter ensures the
        rag/source-populated context — e2e Option A: adapter owns the rs-ensure
        await; the ``universal_catalog.catalog_entries`` substrate stays sync).

        This is the ``(enumerate-all, content_fence)`` **cell**: the shared
        ``enumerate-all`` exposure decides *what* is shown (catalog only, per the
        deviation this cell declares — see ``_enumerate_exposure``), and the
        ``content_fence`` encoder decides *how*.

        #1618 root-1: the dispatchable catalog is the FULL entry list, not the
        exposed subset — CodeAct advertises ∅ but dispatches everything, and
        excluded actions stay IN the dispatchable set so an in-code call to one
        gets the clear ``tool_excluded`` message from the per-call gate rather
        than ``unknown_tool``.

        Excluded-tool *omission from the code-API* is defense-in-depth, NOT the safety
        boundary: the real gate is the per-call exclude + ``dispatch_tool`` re-entry
        in ``execute`` (a code call to an excluded action is rejected at dispatch).
        #3378: the omission reads the session's EFFECTIVE contextual narrowing
        (``available['contextual_permission']``) — the same source the live gate
        enforces — instead of the ``exclude_tools`` name set, which could not express a
        topology / delegate / ephemeral narrowing (so a denied action stayed rendered in
        the code-API) nor an allow-list."""
        entries = await ops.catalog_entries()  # canonical (OpenAI-nested) shape
        exposure = build_enumerate_all_exposure(
            catalog_entries=entries,
            available=available,
            layer_ctx=layer_ctx,
            ops=ops,
            deviation=CONTENT_FENCE_EXPOSURE_DEVIATION,
        )
        return exposure, entries


# #1608: self-register on import (P7 — the OS resolve no longer names this class).
register_scheme(CodeActScheme())

__all__ = ["CodeActScheme"]
