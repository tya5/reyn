"""Transport axis + valid-(scheme,transport) registry (FP-0066 P4a, #3247).

Tool-use decomposes into two orthogonal axes (FP-0066 §2): **scheme**
(presentation — how capabilities are shown/discovered: ``category`` /
``enumerate-all`` / ``retrieval``) x **transport** (how the model expresses the
chosen action: ``tool_calls`` / ``content_fence``). Today the two axes are
conflated in a single flat ``_SCHEMES`` registry (``reyn.tools.scheme``) where
``codeact`` is really the ``content_fence`` transport applied to the
``enumerate-all`` presentation, registered as if it were a 4th sibling scheme.

This module is **internal-only and behavior-preserving** (P4a/P4c): it
introduces the ``Transport`` type and a registry that names which (scheme,
transport) cells are actually resolvable. P4b (config surface, #3247) uses
this registry as the live parse-time validation authority for the
``tool_use.scheme`` x ``tool_use.transport`` 2-key config (the former
``tool_use.chat`` is removed, clean-break). WITHOUT physically splitting
``ToolUseScheme`` into two protocols (firm §2 J3 — ``Presentation`` already
carries the transport freedom via its ``tools_channel`` arm (#3421 — an
absent ``tools=`` channel is a value, not an empty list) + ``tool_use_sp`` + the ``Interpretation`` Execute/CodeBlock branch; transport
is expressed as a construction-strategy + interpret-branch SELECTION, not a
bundle reshuffle).

P4c (#3247) completed the clean-break: ``codeact`` is no longer a name in
``reyn.tools.scheme._SCHEMES`` (and is therefore no longer independently
config-selectable — it never was, since ``tool_use.scheme`` only accepts
presentation-axis names, but this closes the ``_SCHEMES``-level naming gap
too). The (enumerate-all, content_fence) cell's implementation (still the
same ``CodeActScheme`` class — presentation-construction over the enum-all
catalog + the CodeBlock interpret-branch, byte-identical logic) now
self-registers under ``CONTENT_FENCE_ENUMERATE_ALL_SCHEME_NAME`` below, a
name reachable ONLY by resolving ``("enumerate-all", Transport.CONTENT_FENCE)``
through this module's registry — not a name an operator or a test can select
directly by typing "codeact".

Only two transports are implemented today (``tool_calls``, ``content_fence``);
a third (``structured_output``, ``response_format``/json_schema) is deferred to
#3249 — no reserved slot is added here (YAGNI, firm §3 P4d).

Splitting scheme x transport into two axes does not make every conceivable cell
resolvable: everything x a future ``structured_output`` stays UNIMPLEMENTED, and
so does any presentation name that is not in the table below.
``resolve_scheme_for_transport`` is fail-closed on an unregistered cell —
mirrors the #3026 "enumeration is not resolution" pin
(``reyn.tools.universal_catalog``): splitting an axis / set does not silently
widen what is actually resolvable.

#3376 P2 added ``category`` x ``content_fence`` and P3 added ``retrieval`` x
``content_fence`` — the two cells that were *composed* rather than adopted from a
pre-existing implementation, which the Exposure/Encoder seam (P1) is what made
possible: a presentation's defining property is decided in its exposure, so the
``content_fence`` encoder renders whatever that exposure already settled and the
property survives the transport change. Adding a cell here is therefore a
registration plus an exposure, not a new transport implementation. With P3 the
current presentation axis x transport axis product is fully populated; a cell
becomes unregistered again the moment either axis gains a value.
"""
from __future__ import annotations

from enum import Enum


class Transport(str, Enum):
    """How the model expresses a chosen action. Two implemented values only —
    see the module docstring for why ``structured_output`` is deliberately
    absent (#3249, deferred, no reserved slot)."""

    TOOL_CALLS = "tool_calls"
    CONTENT_FENCE = "content_fence"


# P4c (#3247): the ``_SCHEMES`` registry name for the (enumerate-all,
# content_fence) cell's resolved implementation — the class formerly
# self-registered under the bare name ``"codeact"`` as if it were a 4th
# sibling scheme. NOT a valid ``tool_use.scheme`` value (the presentation
# axis only accepts "category" / "enumerate-all" / "retrieval") and not an
# independently-selectable ``_SCHEMES`` name either — reachable ONLY via
# ``resolve_scheme_for_transport("enumerate-all", Transport.CONTENT_FENCE)``.
# Exported so ``reyn.tools.schemes.codeact`` registers its (unmodified)
# ``CodeActScheme`` instance under this exact key — single source of truth,
# no duplicated literal between the registry and the registrant.
CONTENT_FENCE_ENUMERATE_ALL_SCHEME_NAME = "enumerate-all+content_fence"

# #3376 P2: the ``_SCHEMES`` name for the (category, content_fence) cell — the
# ``category`` presentation's folded wrapper surface expressed as a code-API.
# Same construction as the constant above: not a ``tool_use.scheme`` value an
# operator can type, reachable ONLY via
# ``resolve_scheme_for_transport("category", Transport.CONTENT_FENCE)``.
CONTENT_FENCE_CATEGORY_SCHEME_NAME = "category+content_fence"

# #3376 P3: the ``_SCHEMES`` name for the (retrieval, content_fence) cell — the
# search-first surface expressed as a code-API. Same construction as the two
# constants above: not a ``tool_use.scheme`` value an operator can type,
# reachable ONLY via
# ``resolve_scheme_for_transport("retrieval", Transport.CONTENT_FENCE)``.
CONTENT_FENCE_RETRIEVAL_SCHEME_NAME = "retrieval+content_fence"


# The valid-(scheme, transport) registry (firm §2 J1) — the ONLY resolvable
# cells, explicitly enumerated from the FP-0066 §1 census of the current
# `_SCHEMES` registry (`reyn.tools.scheme._SCHEMES`):
#
#   presentation \ transport | tool_calls | content_fence
#   -------------------------|------------|---------------
#   category                 | universal-category | category+content_fence (#3376 P2)
#   enumerate-all             | enumerate-all      | enumerate-all+content_fence (CodeAct)
#   retrieval                 | retrieval          | retrieval+content_fence (#3376 P3)
#
# "category" / "enumerate-all" / "retrieval" here are the FP-0066 §2
# presentation-axis names; the value is the name currently registered in
# `reyn.tools.scheme._SCHEMES` that implements that (scheme, transport) cell
# TODAY. The (enumerate-all, content_fence) cell is CodeAct — census-verified
# as `enumerate-all` presentation (identical full flat catalog via
# `dispatchable_catalog=entries` / `ops.catalog_entries()`) expressed over the
# `content_fence` transport (`tools_channel=NoToolsChannel()` + `tool_use_sp` rendered
# code-API + the CodeBlock interpret branch) — see FP-0066 issue #3247, P4
# firm §1. P4c (#3247) removed the standalone `"codeact"` `_SCHEMES` name
# (clean-break: codeact is reached ONLY via this (scheme, transport) pair,
# never as a scheme name of its own); the same unmodified implementation now
# self-registers under `CONTENT_FENCE_ENUMERATE_ALL_SCHEME_NAME`.
_VALID_SCHEME_TRANSPORT_PAIRS: "dict[tuple[str, Transport], str]" = {
    ("category", Transport.TOOL_CALLS): "universal-category",
    ("category", Transport.CONTENT_FENCE): CONTENT_FENCE_CATEGORY_SCHEME_NAME,
    ("enumerate-all", Transport.TOOL_CALLS): "enumerate-all",
    ("enumerate-all", Transport.CONTENT_FENCE): CONTENT_FENCE_ENUMERATE_ALL_SCHEME_NAME,
    ("retrieval", Transport.TOOL_CALLS): "retrieval",
    ("retrieval", Transport.CONTENT_FENCE): CONTENT_FENCE_RETRIEVAL_SCHEME_NAME,
}


def resolve_scheme_for_transport(scheme: str, transport: Transport) -> str:
    """Resolve a (presentation-scheme, transport) pair to the ``_SCHEMES`` name
    that implements it today — fail-closed (firm §2 J1): an unregistered cell
    (a presentation name that is not on the axis at all, anything x a future
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
    "CONTENT_FENCE_CATEGORY_SCHEME_NAME",
    "CONTENT_FENCE_ENUMERATE_ALL_SCHEME_NAME",
    "CONTENT_FENCE_RETRIEVAL_SCHEME_NAME",
    "resolve_scheme_for_transport",
    "valid_scheme_transport_pairs",
]
