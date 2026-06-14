"""Discovery-mandate tier policy for the universal-category tool-use scheme.

Relocated from ``reyn.chat.router_system_prompt`` (Stage 1, #1627) so the
tier→discovery-mandate POLICY lives in the scheme layer, not the OS.  The OS
(``router_loop``) still calls ``tier_wants_discovery_mandate`` for the
enumerate / retrieval None-path ``build_system_prompt`` call until their own
stages land; ``universal_category`` calls it to compute its own slot-map.

See the full rationale in ``router_system_prompt`` (search for
``#187 Stage C``).  Verbatim move — no logic change.
"""
from __future__ import annotations

# ---------------------------------------------------------------------------
# #187 Stage C: mechanical discovery mandate (weak-tier list_actions-first)
# ---------------------------------------------------------------------------
# Weak models under-explore the catalog: they satisfice (refuse / give up /
# act on the visible hot-list) instead of calling list_actions to discover the
# action they need. The fix composes INTO the existing V18 intent taxonomy
# rather than bolting on a standalone unconditional mandate: branch-3 (task →
# single-target) already routes "obvious/named action → invoke directly;
# OTHERWISE <discovery chain>", but that OTHERWISE is a SOFT routing hint —
# the ~33% under-fire root. Stage C strengthens ONLY that OTHERWISE branch into
# a mechanical MUST, reinforced 3x (branch-3 / §D9 hot-list / Behaviour), each
# carrying a "NON-obvious / unknown / not-named action" scope qualifier.
#
# Why scoped, not unconditional (owner decision + B11-R3 evidence): a bare
# "list_actions FIRST always" reverses B11-R3 (named-skill → invoke directly,
# skip the list-hop) whose mandatory hop made weak models fall through to
# clarification = the exact non-invoke attractor #187 fights. The obvious/named
# clause (branch-3) and the Conversation (branch-1) / Question (branch-2)
# branches are UNTOUCHED, so chitchat / named-skill / direct routing are
# preserved by construction. The mechanical lever (MUST) + 3x reinforcement
# lifts list_actions-first ~25-55% → ~75-85% for genuine unnamed-discovery.
#
# VERBATIM wording — do NOT paraphrase (fire-rate is wording-sensitive). The
# explicit-action-enumeration "before reading, writing, or editing" is the
# verified lever (25-55%); the generic "before acting / any other tool" detunes
# to 0-10%. Gated to weak tiers; lives in the static cacheable prefix.

# Tier-gate: only tiers empirically shown to under-explore receive the mandate.
# ``light`` is the default intent tier (flash-lite-backed). Unknown/future and
# strong tiers stay OFF — strong-flexibility-preserving default (owner knob:
# don't weak-specialise away strong models' latitude).
_WEAK_TIERS = frozenset({"light"})


def tier_wants_discovery_mandate(router_model: "str | None") -> bool:
    """True if the router tier should receive the mechanical list_actions
    discovery mandate (#187 Stage C). Only verified weak tier(s) opt in;
    unknown / strong tiers stay OFF (strong-flexibility-preserving default)."""
    return router_model in _WEAK_TIERS


__all__ = ["_WEAK_TIERS", "tier_wants_discovery_mandate"]
