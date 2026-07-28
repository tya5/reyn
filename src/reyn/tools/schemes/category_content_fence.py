"""CategoryContentFenceScheme — the (category, content_fence) cell (#3376 P2).

The ``category`` presentation's small, catalog-size-independent surface,
expressed over the ``content_fence`` transport: the model is shown a Python
code-API of the **wrappers** (``list_actions`` / ``describe_action`` /
``invoke_action``) plus the base tools, and it acts by writing a fenced snippet
that calls one of them — ``invoke_action(action_name="file__read", args={...})``
rather than a native tool call.

**Why this cell could not be composed before.** ``category``'s defining
invariant is that the LLM-visible surface does not grow with the catalog, and
the pre-#3376 code put the transport's encoding decision inside each scheme, so
"category over content_fence" had no place to happen except by enumerating the
catalog — which would have dissolved the invariant. The Exposure/Encoder seam
moves the fold to the exposure (``_category_exposure``), so the encoder here
renders **N wrappers, not M actions**: the same set the ``tool_calls`` cell
advertises, written as function signatures instead of a ``tools=`` array.

**Use when** a weak / low-cost model does better writing code than emitting JSON
tool calls (the ``content_fence`` reason) *and* the catalog is large enough that
listing every action up front is the wrong trade (the ``category`` reason). The
``enumerate-all`` content_fence cell (CodeAct) gives up the second.

Everything except the exposure is the transport, inherited from
``ContentFenceCellScheme``: fence extraction, sandboxed execution through the OS
per-call gate, and the observation turn. In particular the per-call gate is the
same one the ``tool_calls`` cell goes through — ``_excluded_result`` unwraps
``invoke_action`` to its effective action name before the exclusion check, and
``dispatch_tool`` then runs the wrapper handler — so wrapping the call in Python
neither widens nor narrows what the model may do.
"""
from __future__ import annotations

from typing import Any

from reyn.tools.exposure import Exposure
from reyn.tools.scheme import register_scheme
from reyn.tools.schemes._category_exposure import (
    CONTENT_FENCE_EXPOSURE_DEVIATION,
    build_category_exposure,
)
from reyn.tools.schemes._content_fence_cell import ContentFenceCellScheme
from reyn.tools.transport import CONTENT_FENCE_CATEGORY_SCHEME_NAME


class CategoryContentFenceScheme(ContentFenceCellScheme):
    """The ``category`` presentation over the ``content_fence`` transport.

    ``name`` is a ``_SCHEMES`` key reachable ONLY by resolving
    ``("category", Transport.CONTENT_FENCE)`` through the valid-pair registry —
    it is not a ``tool_use.scheme`` value an operator can type, the same
    construction the ``enumerate-all`` content_fence cell uses."""

    name: str = CONTENT_FENCE_CATEGORY_SCHEME_NAME

    async def build_exposure(
        self, available: Any, layer_ctx: Any, ops: Any,
    ) -> "tuple[Exposure, list[dict]]":
        """The shared ``category`` exposure — the folded wrapper set, not the catalog.

        The dispatchable catalog is the wrapper set: unlike the ``enumerate-all``
        content_fence cell (which advertises ∅ but dispatches every action), here
        the wrappers ARE the dispatchable names, and each real action is reached
        through ``invoke_action``'s own handler.

        ★ It is the PRE-narrowing list, not ``exposure.descriptors``, and the two
        differ whenever the narrowing below hides a row. Two reasons, both
        load-bearing: the encoder derives the code-API's identifiers from
        ``exposure.dispatchable_names`` while ``execute`` derives the sandbox
        stubs' names from this list, so a narrowed list would shift a collision
        suffix and the model would call an identifier no stub answers to; and a
        narrowed dispatch gate would answer an in-code call to a hidden action
        with ``unknown_tool`` instead of the truthful ``tool_excluded`` (#1618
        root-1).

        The narrowing declared by ``CONTENT_FENCE_EXPOSURE_DEVIATION`` is applied
        inside the exposure builder rather than by the OS's post-presentation
        ``tools=`` filter, because this transport has no ``tools=`` payload for
        that filter to act on."""
        present_entries = list(ops.present(available, layer_ctx).llm_tools_payload)
        exposure = build_category_exposure(
            present_entries=present_entries,
            available=available,
            layer_ctx=layer_ctx,
            deviation=CONTENT_FENCE_EXPOSURE_DEVIATION,
        )
        return exposure, present_entries


# Self-register on import (P7 — the OS resolve names no scheme class).
register_scheme(CategoryContentFenceScheme())

__all__ = ["CategoryContentFenceScheme"]
