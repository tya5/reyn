"""RetrievalContentFenceScheme — the (retrieval, content_fence) cell (#3376 P3).

The ``retrieval`` presentation's search-first surface, expressed over the
``content_fence`` transport: the model is shown a Python code-API that leads with
``search_actions`` — never a listing of the catalog — and it acts by writing a
fenced snippet that searches and then calls what the search returned::

    hits = search_actions(query="read a file")
    result = invoke_action(action_name="read_file", args={"path": "README.md"})

**Why this cell needs no ``RePresent``, and could not use one.** The
``tool_calls`` cell narrows by re-presentation: its search affordance is
synthetic, ``interpret`` intercepts the call, and the OS swaps the ``tools=``
payload so the matched actions become directly callable. That move is unavailable
here — this transport's whole tool-use surface is the system prompt, and the
system prompt is built once per turn (``router_loop.py`` assembles ``messages[0]``
before the iteration loop; the ``RePresent`` arm swaps ``tools=`` and the dispatch
catalog, not the prompt). So a re-presented code-API would go nowhere.

★ It does not have to. ``RePresent`` is a workaround for a payload that can only
change *between* LLM calls; a code-API narrows **at runtime, inside one snippet**,
because the search's result is an ordinary value in the model's code. The
narrowing that costs the ``tool_calls`` cell a round trip is free here. That is
why the pair is worth filling rather than being a symmetry exercise: the same
paradigm, one round trip cheaper.

**What is exposed, and the one thing that is withheld.** ``ops.present`` composes
the base tools with the catalog wrappers; this cell renders all of them except
``list_actions``. Withholding the enumeration affordance is the whole difference
from the ``(category, content_fence)`` cell: both keep the surface small, but
``category`` lets the model browse the catalog by name while ``retrieval``
requires it to describe what it wants. ``describe_action`` stays — the model
learns action NAMES from the search and still needs their argument schemas before
``invoke_action`` can be called correctly.

Omission from the code-API is presentation, not the safety boundary: the
dispatchable set is the pre-narrowing list, so an in-code ``list_actions()`` call
still resolves and is answered by the same per-call gate as everything else. The
boundary is that gate (``_content_fence_cell.execute``), exactly as for the other
cells on this transport.

**Degraded branch.** When the D14 gate says the search is not usable (no
embedding provider, index not ready yet), presenting a ``search_actions`` the
model cannot get results from would strand it — the #2895 dead-session shape,
which the ``tool_calls`` cell guards against the same way. This cell degrades
like ``enumerate-all``: it renders the base tools plus the flat catalog, so
everything stays reachable directly. The ``tool_calls`` cell also injects
``_HIDDEN_STATE_HINT`` in that branch, and this one deliberately does not — that
text explains why a wrapper the model can see is returning nothing, and in this
branch there is no wrapper and nothing hidden: every action is listed by name.
(It could not be carried anyway: ``sp_slot_overrides`` is a positional-slot
channel and the ``content_fence`` encoder refuses one rather than dropping it.)
"""
from __future__ import annotations

from typing import Any

from reyn.tools.exposure import (
    Exposure,
    ExposureDeviation,
    descriptors_from_entries,
    without_duplicate_names,
)
from reyn.tools.scheme import advertised_entries, register_scheme
from reyn.tools.schemes._content_fence_cell import ContentFenceCellScheme
from reyn.tools.schemes._retrieval_exposure import retrieval_sp_facts
from reyn.tools.transport import CONTENT_FENCE_RETRIEVAL_SCHEME_NAME

#: The enumeration affordance this presentation withholds. ``search_actions`` is
#: the entry point; ``describe_action`` and ``invoke_action`` are what the model
#: needs *after* a hit, so only the browse-by-category verb is dropped.
_ENUMERATION_AFFORDANCE = frozenset({"list_actions"})

SEARCH_FIRST_EXPOSURE_DEVIATION = ExposureDeviation(
    includes_base_tools=True,
    excluded_names=_ENUMERATION_AFFORDANCE,
    applies_contextual_narrowing=True,
    rationale=(
        "Base tools + the catalog wrappers as ``build_tools`` composes them, minus "
        "``list_actions``: discovery in this presentation is a search, not a "
        "listing, and leaving the browse verb in would give the model a way to "
        "enumerate the catalog that the scheme exists to avoid. It is withheld "
        "from the code-API only — it stays in the dispatchable set, so an in-code "
        "call to it is answered by the per-call gate rather than by "
        "``unknown_tool``. Contextual narrowing IS applied here because this "
        "cell's whole surface is a rendered string and nothing downstream narrows "
        "a string; the OS's post-presentation filter runs over ``tools=``, which "
        "this transport does not have."
    ),
)

DEGRADED_EXPOSURE_DEVIATION = ExposureDeviation(
    includes_base_tools=True,
    excluded_names=frozenset(),
    applies_contextual_narrowing=True,
    rationale=(
        "The #2895 runtime auto-fallback: the D14 gate reports the search is not "
        "usable, so the search-first surface would strand the model on an "
        "affordance that returns nothing. Base tools + the flat catalog instead — "
        "nothing is reachable only through a search that cannot run. Nothing is "
        "excluded because nothing is being hidden behind a wrapper in this branch."
    ),
)


def _entry_name(entry: dict) -> str:
    body = entry.get("function")
    return str((body if isinstance(body, dict) else entry).get("name", ""))


def build_retrieval_content_fence_exposure(
    *,
    entries: "list[dict]",
    available: Any,
    layer_ctx: Any,
    deviation: ExposureDeviation,
) -> Exposure:
    """Build the transport-neutral exposure for the ``content_fence`` retrieval cell.

    ``entries`` is the cell's full population — the wrapper composition in the
    search-first branch, base tools + the flat catalog in the degraded one. It is
    passed in already computed because the caller needs the same untouched list
    for its dispatchable catalog, and rebuilding it would be a second answer as
    well as a second call."""
    if not deviation.includes_base_tools:
        # A declaration that cannot be honoured must not pass silently. Both
        # branches obtain their base tools inside the same composition they
        # obtain everything else from, so this cell has no seam at which base
        # tools could be dropped — refusing keeps the field a checked claim
        # rather than a decorative one (same reasoning as ``_category_exposure``).
        raise ValueError(
            "the retrieval content_fence cell cannot exclude base tools: both of "
            "its branches receive them inside one composed entry list, so "
            "includes_base_tools=False has no seam to act at."
        )

    all_entries = list(entries)

    # The executor's universe: every name this cell can dispatch, BEFORE the
    # exclusion and the narrowing below. A fact, so it belongs here; the
    # identifier map derived from it is an encoding, so it does not.
    dispatchable_names = tuple(n for n in (_entry_name(e) for e in all_entries) if n)

    exposed = without_duplicate_names(
        [e for e in all_entries if _entry_name(e) not in deviation.excluded_names]
    )
    if deviation.applies_contextual_narrowing:
        contextual = (available or {}).get("contextual_permission")
        if contextual is not None:
            from reyn.security.permissions.effective import tool_contextually_denied

            exposed = [
                e for e in exposed if not tool_contextually_denied(contextual, _entry_name(e))
            ]

    return Exposure(
        descriptors=descriptors_from_entries(exposed),
        sp_facts=retrieval_sp_facts(layer_ctx),
        dispatchable_names=dispatchable_names,
        deviation=deviation,
    )


class RetrievalContentFenceScheme(ContentFenceCellScheme):
    """The ``retrieval`` presentation over the ``content_fence`` transport.

    ``name`` is a ``_SCHEMES`` key reachable ONLY by resolving
    ``("retrieval", Transport.CONTENT_FENCE)`` through the valid-pair registry —
    it is not a ``tool_use.scheme`` value an operator can type, the same
    construction the other two ``content_fence`` cells use."""

    name: str = CONTENT_FENCE_RETRIEVAL_SCHEME_NAME

    async def build_exposure(
        self, available: Any, layer_ctx: Any, ops: Any,
    ) -> "tuple[Exposure, list[dict]]":
        """The search-first exposure, or the degraded one when the search is dead.

        ``search_visible`` is the OS's D14 gate (embedding model class configured
        AND an ``ActionEmbeddingIndex`` that ``is_ready()`` AND the universal
        wrappers on). It is read rather than re-derived because it is the SAME
        signal that decided whether ``ops.present`` composed a ``search_actions``
        wrapper at all — deriving a second answer here could present a search the
        router did not build.

        The dispatchable catalog is the PRE-narrowing list, not
        ``exposure.descriptors``, and the two differ whenever ``list_actions`` or
        a contextual denial hides a row. Two reasons, both load-bearing: the
        encoder derives the code-API's identifiers from
        ``exposure.dispatchable_names`` while ``execute`` derives the sandbox
        stubs' names from this list, so a narrowed list would shift a collision
        suffix and the model would call an identifier no stub answers to; and a
        narrowed dispatch gate would answer an in-code call to a hidden action
        with ``unknown_tool`` instead of the truthful ``tool_excluded``
        (#1618 root-1)."""
        if layer_ctx.get("search_visible", False):
            entries = advertised_entries(ops.present(available, layer_ctx).tools_channel)
            deviation = SEARCH_FIRST_EXPOSURE_DEVIATION
        else:
            entries = list(ops.base_tools(available, layer_ctx)) + await ops.catalog_entries()
            deviation = DEGRADED_EXPOSURE_DEVIATION
        exposure = build_retrieval_content_fence_exposure(
            entries=entries,
            available=available,
            layer_ctx=layer_ctx,
            deviation=deviation,
        )
        return exposure, entries


# Self-register on import (P7 — the OS resolve names no scheme class).
register_scheme(RetrievalContentFenceScheme())

__all__ = [
    "DEGRADED_EXPOSURE_DEVIATION",
    "SEARCH_FIRST_EXPOSURE_DEVIATION",
    "RetrievalContentFenceScheme",
    "build_retrieval_content_fence_exposure",
]
