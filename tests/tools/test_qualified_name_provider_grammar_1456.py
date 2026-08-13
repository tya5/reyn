"""Tier 2: #1456 (c) canary — action names obey provider function-name grammar.

#1456 renamed the 5 dotted categories (``memory.entry`` → ``memory_entry``
etc.) so the whole namespace is grammar-safe. The tightest provider grammar
we target is OpenAI's:

    ^[a-zA-Z0-9_-]{1,64}$   (Anthropic allows 128; Gemini is alnum/_/- too)

Dots are outside ALL three specs; this canary pins that **by construction** —
re-introducing a dotted (or otherwise illegal) name fails here, not silently
at a strict provider's API at call time.

Note: the hot-list alias builder (``router_loop._build_hot_list_aliases``),
which used to turn a ``DEFAULT_HOT_LIST_SEED`` action name into an OpenAI
function ``"name"`` verbatim, was removed along with the hot-list feature
(#4552 PR-1). The tests below now cover the surfaces that remain reachable
by the LLM as a name-shaped value:
  - KNOWN_ACTION_NAMES (every action the LLM can address, e.g. via
    ``invoke_action``)
  - CATEGORIES — no longer a name PREFIX since #3429 abolished the qualified
    spelling, but still LLM-visible as the ``category=[…]`` enum on
    ``list_actions`` / ``search_actions``, so it stays checked.
"""
from __future__ import annotations

import re

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


def test_action_names_are_grammar_safe() -> None:
    """Tier 2: #1456 — every action name the LLM can address (and that can
    surface as a hot-list entry) matches the grammar."""
    bad = [k for k in sorted(KNOWN_ACTION_NAMES) if not _FUNCTION_NAME_RE.match(k)]
    assert bad == [], f"action names violate function-name grammar: {bad}"


def test_no_dotted_names_anywhere_in_the_static_surface() -> None:
    """Tier 2: #1456 — the decisive guard: no dot in any category / action
    name. Dots are the specific violation #1456 removed; this fails fast
    if a dotted name is reintroduced anywhere in the canonical static surface."""
    surface = list(CATEGORIES) + sorted(KNOWN_ACTION_NAMES)
    dotted = [n for n in surface if "." in n]
    assert dotted == [], f"dotted names reintroduced (provider-grammar risk): {dotted}"
