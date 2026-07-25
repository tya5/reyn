"""Transport axis + valid-(scheme,transport) registry (FP-0066 P4a, #3247).

Tool-use decomposes into two orthogonal axes (FP-0066 §2): **scheme**
(presentation — how capabilities are shown/discovered: ``category`` /
``enumerate-all`` / ``retrieval``) x **transport** (how the model expresses the
chosen action: ``tool_calls`` / ``content_fence``). Today the two axes are
conflated in a single flat ``_SCHEMES`` registry (``reyn.tools.scheme``) where
``codeact`` is really the ``content_fence`` transport applied to the
``enumerate-all`` presentation, registered as if it were a 4th sibling scheme.

This module is **internal-only and behavior-preserving** (P4a): it introduces
the ``Transport`` type and a registry that names which (scheme, transport)
cells are actually resolvable. P4b (config surface, #3247) now uses this
registry as the live parse-time validation authority for the
``tool_use.scheme`` x ``tool_use.transport`` 2-key config (the former
``tool_use.chat`` is removed, clean-break). P4a itself is WITHOUT removing
``codeact`` from ``_SCHEMES`` (P4c), and WITHOUT physically splitting
``ToolUseScheme`` into two protocols (firm §2 J3 —
``Presentation`` already carries the transport freedom via
``llm_tools_payload`` emptiness + ``tool_use_sp`` + the ``Interpretation``
Execute/CodeBlock branch; transport is expressed as a construction-strategy +
interpret-branch SELECTION, not a bundle reshuffle).

Only two transports are implemented today (``tool_calls``, ``content_fence``);
a third (``structured_output``, ``response_format``/json_schema) is deferred to
#3249 — no reserved slot is added here (YAGNI, firm §3 P4d).

Splitting scheme x transport into two axes does not make the full 3x2 cartesian
product resolvable: ``category`` x ``content_fence`` and everything x a future
``structured_output`` are UNIMPLEMENTED cells. ``resolve_scheme_for_transport``
is fail-closed on an unregistered cell — mirrors the #3026 "enumeration is not
resolution" pin (``reyn.tools.universal_catalog``): splitting an axis / set does
not silently widen what is actually resolvable.
"""
from __future__ import annotations

from enum import Enum


class Transport(str, Enum):
    """How the model expresses a chosen action. Two implemented values only —
    see the module docstring for why ``structured_output`` is deliberately
    absent (#3249, deferred, no reserved slot)."""

    TOOL_CALLS = "tool_calls"
    CONTENT_FENCE = "content_fence"


# The valid-(scheme, transport) registry (firm §2 J1) — the ONLY resolvable
# cells, explicitly enumerated from the FP-0066 §1 census of the current
# `_SCHEMES` registry (`reyn.tools.scheme._SCHEMES`):
#
#   presentation \ transport | tool_calls | content_fence
#   -------------------------|------------|---------------
#   category                 | universal-category | (unimplemented)
#   enumerate-all             | enumerate-all      | codeact
#   retrieval                 | retrieval          | (unimplemented)
#
# "category" / "enumerate-all" / "retrieval" here are the FP-0066 §2
# presentation-axis names; the value is the name currently registered in
# `reyn.tools.scheme._SCHEMES` that implements that (scheme, transport) cell
# TODAY (byte-identical — P4a moves no logic). `codeact` census-verified as
# `enumerate-all` presentation (identical full flat catalog via
# `dispatchable_catalog=entries` / `ops.catalog_entries()`) expressed over the
# `content_fence` transport (`llm_tools_payload=[]` + `tool_use_sp` rendered
# code-API + the CodeBlock interpret branch) — see FP-0066 issue #3247, P4
# firm §1.
_VALID_SCHEME_TRANSPORT_PAIRS: "dict[tuple[str, Transport], str]" = {
    ("category", Transport.TOOL_CALLS): "universal-category",
    ("enumerate-all", Transport.TOOL_CALLS): "enumerate-all",
    ("enumerate-all", Transport.CONTENT_FENCE): "codeact",
    ("retrieval", Transport.TOOL_CALLS): "retrieval",
}


def resolve_scheme_for_transport(scheme: str, transport: Transport) -> str:
    """Resolve a (presentation-scheme, transport) pair to the ``_SCHEMES`` name
    that implements it today — fail-closed (firm §2 J1): an unregistered cell
    (e.g. ``category`` x ``content_fence``, anything x a future
    ``structured_output``) raises a legible ``ValueError`` rather than being
    silently allowed or silently falling back to a default. A silently-accepted
    unregistered cell is exactly the "configuration doesn't do what it says"
    trap the firm's J1 calls out (mirrors #3026 "enumeration is not
    resolution": splitting the axis in two does not widen the resolvable set
    past what is explicitly registered)."""
    try:
        return _VALID_SCHEME_TRANSPORT_PAIRS[(scheme, transport)]
    except KeyError:
        valid = ", ".join(
            f"({s!r}, {t.value!r})" for s, t in valid_scheme_transport_pairs()
        )
        raise ValueError(
            f"no (scheme, transport) registration for (scheme={scheme!r}, "
            f"transport={transport.value!r}); valid pairs: {valid}"
        ) from None


def valid_scheme_transport_pairs() -> "list[tuple[str, Transport]]":
    """Sorted ``(scheme, transport)`` keys of the valid-pair registry
    (introspection / tests)."""
    return sorted(_VALID_SCHEME_TRANSPORT_PAIRS, key=lambda pair: (pair[0], pair[1].value))


__all__ = [
    "Transport",
    "resolve_scheme_for_transport",
    "valid_scheme_transport_pairs",
]
