"""Tier 2: #1456 (c) canary — qualified names obey provider function-name grammar.

Hot-list aliases use the qualified name VERBATIM as the OpenAI-format function
name (router_loop `_build_hot_list_aliases` sets ``"name": name`` with no
sanitization — the 1:1 single-namespace design). So every name that can reach a
provider as a function name MUST satisfy the function-name grammar of the
providers we target. The tightest is OpenAI's:

    ^[a-zA-Z0-9_-]{1,64}$   (Anthropic allows 128; Gemini is alnum/_/- too)

Dots are outside ALL three specs. #1456 renamed the 5 dotted categories
(``memory.entry`` → ``memory_entry`` etc.) so the whole namespace is
grammar-safe; this canary pins that **by construction** — re-introducing a
dotted (or otherwise illegal) category / qualified name fails here, not silently
at a strict provider's API at call time.

Sources of wire names checked (the static surface; the alias builder is a
verbatim passthrough, so checking the sources checks the wire):
  - CATEGORIES (the category prefix of every qualified name)
  - DEFAULT_HOT_LIST_SEED (seed aliases → function names directly)
  - _OPERATION_RULES keys (operation-category full qualified names)
  - _RESOURCE_RULES keys (resource-category prefixes)
"""
from __future__ import annotations

import re

from reyn.tools.action_usage_tracker import DEFAULT_HOT_LIST_SEED
from reyn.tools.universal_catalog import CATEGORIES
from reyn.tools.universal_dispatch import _OPERATION_RULES, _RESOURCE_RULES

# OpenAI's function-name grammar — the tightest of the providers we target.
_FUNCTION_NAME_RE = re.compile(r"^[a-zA-Z0-9_-]{1,64}$")
# A category is the prefix before ``__`` — same character class, unbounded
# length here (the full qualified name is what the 64-char cap applies to).
_CATEGORY_RE = re.compile(r"^[a-zA-Z0-9_-]+$")


def test_categories_are_grammar_safe() -> None:
    """Tier 2: #1456 — every CATEGORIES entry is alnum/_/- (no dots).

    A category is the prefix of every qualified name in that category, so an
    illegal category poisons every name under it."""
    bad = [c for c in CATEGORIES if not _CATEGORY_RE.match(c)]
    assert bad == [], f"categories violate provider function-name grammar: {bad}"


def test_hot_list_seed_names_are_grammar_safe() -> None:
    """Tier 2: #1456 — every DEFAULT_HOT_LIST_SEED name matches the function-name
    grammar (these become function names verbatim via the alias builder)."""
    bad = [n for n in DEFAULT_HOT_LIST_SEED if not _FUNCTION_NAME_RE.match(n)]
    assert bad == [], f"hot-list seed names violate function-name grammar: {bad}"


def test_operation_routing_keys_are_grammar_safe() -> None:
    """Tier 2: #1456 — every _OPERATION_RULES key (a full qualified name the LLM
    can address / that can surface as a hot-list alias) matches the grammar."""
    bad = [k for k in _OPERATION_RULES if not _FUNCTION_NAME_RE.match(k)]
    assert bad == [], f"operation routing keys violate function-name grammar: {bad}"


def test_resource_routing_keys_are_grammar_safe() -> None:
    """Tier 2: #1456 — every _RESOURCE_RULES key (a resource-category prefix)
    is grammar-safe, so any qualified name built under it is too."""
    bad = [k for k in _RESOURCE_RULES if not _CATEGORY_RE.match(k)]
    assert bad == [], f"resource routing keys violate function-name grammar: {bad}"


def test_no_dotted_names_anywhere_in_the_static_surface() -> None:
    """Tier 2: #1456 — the decisive guard: no dot in any category / seed /
    routing key. Dots are the specific violation #1456 removed; this fails fast
    if a dotted name is reintroduced anywhere in the canonical static surface."""
    surface = (
        list(CATEGORIES)
        + list(DEFAULT_HOT_LIST_SEED)
        + list(_OPERATION_RULES)
        + list(_RESOURCE_RULES)
    )
    dotted = [n for n in surface if "." in n]
    assert dotted == [], f"dotted names reintroduced (provider-grammar risk): {dotted}"


def test_alias_builder_drops_wire_unsafe_names() -> None:
    """Tier 2: #1456 (c) runtime-boundary guard — the ONLY emission point where a
    qualified name becomes a tools= function name verbatim (_build_hot_list_aliases)
    drops any name violating the function-name grammar. So a dotted name — a
    collapsed/legacy category (agent.peer__* / mcp.tool__*) or a future dynamic
    prefix — can NEVER reach the wire as a function name, by construction at the
    boundary, independent of whether every upstream source pre-filtered it."""
    from reyn.chat.router_loop import _build_hot_list_aliases

    out = _build_hot_list_aliases([
        "file__read",              # wire-safe → kept
        "agent.peer__alice",       # dotted (collapsed category) → dropped
        "mcp.tool__brave.search",  # dotted → dropped
    ])
    emitted = [d["function"]["name"] for d in out]
    assert "file__read" in emitted
    assert all("." not in n for n in emitted), f"dotted name reached the wire: {emitted}"
    assert "agent.peer__alice" not in emitted
    assert "mcp.tool__brave.search" not in emitted
