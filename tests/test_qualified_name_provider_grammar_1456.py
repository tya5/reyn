"""Tier 2: #1456 (c) canary — action names obey provider function-name grammar.

Hot-list entries use the action name VERBATIM as the OpenAI-format function
name (router_loop `_build_hot_list_aliases` sets ``"name": name`` with no
sanitization — the 1:1 single-namespace design). So every name that can reach a
provider as a function name MUST satisfy the function-name grammar of the
providers we target. The tightest is OpenAI's:

    ^[a-zA-Z0-9_-]{1,64}$   (Anthropic allows 128; Gemini is alnum/_/- too)

Dots are outside ALL three specs. #1456 renamed the 5 dotted categories
(``memory.entry`` → ``memory_entry`` etc.) so the whole namespace is
grammar-safe; this canary pins that **by construction** — re-introducing a
dotted (or otherwise illegal) name fails here, not silently at a strict
provider's API at call time.

Sources of wire names checked (the static surface; the alias builder is a
verbatim passthrough, so checking the sources checks the wire):
  - KNOWN_ACTION_NAMES (every action the LLM can address)
  - DEFAULT_HOT_LIST_SEED (seed entries → function names directly)
  - CATEGORIES — no longer a name PREFIX since #3429 abolished the qualified
    spelling, but still LLM-visible as the ``category=[…]`` enum on
    ``list_actions`` / ``search_actions``, so it stays checked.
"""
from __future__ import annotations

import re

from reyn.tools.action_usage_tracker import DEFAULT_HOT_LIST_SEED
from reyn.tools.universal_catalog import CATEGORIES
from reyn.tools.universal_dispatch import KNOWN_ACTION_NAMES

# OpenAI's function-name grammar — the tightest of the providers we target.
_FUNCTION_NAME_RE = re.compile(r"^[a-zA-Z0-9_-]{1,64}$")
# A category reaches the model as a schema enum VALUE, not as a function name,
# so only the character class applies (no 64-char function-name cap).
_CATEGORY_RE = re.compile(r"^[a-zA-Z0-9_-]+$")


def test_categories_are_grammar_safe() -> None:
    """Tier 2: #1456 — every CATEGORIES entry is alnum/_/- (no dots)."""
    bad = [c for c in CATEGORIES if not _CATEGORY_RE.match(c)]
    assert bad == [], f"categories violate provider function-name grammar: {bad}"


def test_hot_list_seed_names_are_grammar_safe() -> None:
    """Tier 2: #1456 — every DEFAULT_HOT_LIST_SEED name matches the function-name
    grammar (these become function names verbatim via the alias builder)."""
    bad = [n for n in DEFAULT_HOT_LIST_SEED if not _FUNCTION_NAME_RE.match(n)]
    assert bad == [], f"hot-list seed names violate function-name grammar: {bad}"


def test_action_names_are_grammar_safe() -> None:
    """Tier 2: #1456 — every action name the LLM can address (and that can
    surface as a hot-list entry) matches the grammar."""
    bad = [k for k in sorted(KNOWN_ACTION_NAMES) if not _FUNCTION_NAME_RE.match(k)]
    assert bad == [], f"action names violate function-name grammar: {bad}"


def test_no_dotted_names_anywhere_in_the_static_surface() -> None:
    """Tier 2: #1456 — the decisive guard: no dot in any category / seed /
    action name. Dots are the specific violation #1456 removed; this fails fast
    if a dotted name is reintroduced anywhere in the canonical static surface."""
    surface = (
        list(CATEGORIES)
        + list(DEFAULT_HOT_LIST_SEED)
        + sorted(KNOWN_ACTION_NAMES)
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
    from reyn.runtime.router_loop import _build_hot_list_aliases

    out = _build_hot_list_aliases([
        "read_file",              # wire-safe → kept
        "agent.peer__alice",       # dotted (collapsed category) → dropped
        "mcp.tool__brave.search",  # dotted → dropped
    ])
    emitted = [d["function"]["name"] for d in out]
    assert "read_file" in emitted
    assert all("." not in n for n in emitted), f"dotted name reached the wire: {emitted}"
    assert "agent.peer__alice" not in emitted
    assert "mcp.tool__brave.search" not in emitted
