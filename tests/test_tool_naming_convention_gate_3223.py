"""Tier 2: tool-naming convention drift gate (#3223).

Full rationale + the human-readable convention tables live in
``docs/reference/runtime/tool-naming.md`` — read that doc first; this file
is the executable half of the same source of truth.

#3223's root observation: tool naming has both a STRUCTURAL axis (word
order / shape of the name string — gate-able with zero false positives) and
a SEMANTIC axis (did the author pick the *right* operation-class, e.g.
``delete`` vs ``drop`` — not gate-able without re-implementing code review).
This gate enforces ONLY the structural axis. It does not, and cannot, judge
whether a new tool's class choice was semantically correct.

Owner-ratified policy: no rename sweep. Every name that predates this
convention (or intentionally sits outside it — an established family
prefix, a term-of-art compound, a discriminated-arg install group, ...) is
grandfathered by exact name below, each with a one-line reason. The gate
only constrains NEW names going forward: a name not in the grandfather set
must fit the structural rules, or CI goes red.

This is the "enumerate, don't curate" idiom (#3194/#3075/#3193 precedent):
the registry itself (not a hand-picked subset) is walked, so a newly
registered tool is checked automatically rather than depending on someone
remembering to update a second list.
"""
from __future__ import annotations

# ---------------------------------------------------------------------------
# R1/R2/R3/R4/R5 structural rules — see docs/reference/runtime/tool-naming.md
# ---------------------------------------------------------------------------

# Canonical verb lexicon, reconciled against the LIVE registry census (not
# guessed from the issue brief) — every entry below was derived by walking
# get_default_registry().names() + universal_dispatch._OPERATION_RULES and
# classifying each real name; the main gate tests below re-run that walk on
# every CI run, so lexicon/grandfather drift from the live registry surfaces
# immediately as a test failure rather than silently going stale.
CANONICAL_VERBS: frozenset[str] = frozenset(
    {
        "list", "read", "describe", "delete", "drop", "forget", "uninstall",
        "install_local", "install_source", "install_package", "install_registry",
        "run", "load", "search", "fetch", "spawn", "call", "remember", "render",
        "present", "compact", "ask", "edit", "emit", "glob", "grep", "invoke",
        "subscribe", "unsubscribe", "write", "delegate", "embed", "exec",
    }
)

# R2's frozen 4-verb removal class table. A name using one of these verbs is
# fine; a NEW removal verb outside this set (a "5th verb") is the drift this
# gate exists to catch (see docs/reference/runtime/tool-naming.md § R2 for
# the "why not unify to `remove`" rationale).
REMOVAL_VERBS: frozenset[str] = frozenset({"delete", "drop", "forget", "uninstall"})

# Compound verbs that must be matched as a whole unit before falling back to
# single-token splitting (R4: source-split install is the canonical multi-
# word verb; splitting on the first "_" alone would wrongly read
# "install_local" as verb "install" + object "local", which would then let
# a bare, non-source-split "install" slip through as compliant).
_COMPOUND_VERBS: tuple[str, ...] = (
    "install_local", "install_source", "install_package", "install_registry",
)

# Grandfathered FLAT (single-namespace, registry) names — each frozen with a
# one-line "why exempt" reason. Family-prefix groups per R1; individual
# anomalies per the doc's "Grandfathered anomalies" section.
FLAT_GRANDFATHER: frozenset[str] = frozenset(
    {
        # spawn-family (long-lived entity creation), object_verb order
        "agent_spawn", "session_spawn",
        # cron_* family (R1 family-prefix grandfather)
        "cron_disable", "cron_enable", "cron_list", "cron_register", "cron_unregister",
        # R3 sole pre-existing `get` exception (new `get` is forbidden)
        "get_mcp_prompt",
        # sole hooks_*-shaped tool, predates convention, no established family
        "hooks_add",
        # object_verb order anomaly ("update" the "index"), predates convention
        "index_update",
        # mcp_* family (R1 family-prefix grandfather); mcp_install is additionally
        # an orphan superseded by the install_local/_package/_registry split
        # (2026-05-25) and not present in _OPERATION_RULES any more — flagged
        # as dead surface, candidate for a separate retire follow-up
        "mcp_call_tool", "mcp_drop_server", "mcp_install", "mcp_install_local",
        "mcp_install_package", "mcp_install_registry", "mcp_search_registry",
        # pipeline_* family (R1 family-prefix grandfather)
        "pipeline_install_local", "pipeline_install_source", "pipeline_list",
        # discriminated-arg legacy install group — flat names that additionally
        # use catalog-style "__" inside a flat registry name (pre-existing;
        # not a new mechanism, R4 grandfather)
        "plugin_management__install", "plugin_management__list",
        "plugin_management__uninstall",
        # sole presentation_*-shaped install, predates the source-split convention
        "presentation_install_local",
        # reyn_repo_* family (R1 family-prefix grandfather)
        "reyn_repo_glob", "reyn_repo_grep", "reyn_repo_list", "reyn_repo_read",
        # established term-of-art compound naming the whole RAG retrieval macro
        "semantic_search",
        # skill_* family (R1 family-prefix grandfather)
        "skill_install_local", "skill_install_source", "skill_list",
        # sole tool of its shape, no established family, predates convention
        "topology_create",
        # web_* family (R1 family-prefix grandfather), object_verb order
        "web_fetch", "web_search",
    }
)

# Grandfathered QUALIFIED (catalog "category__verb") names.
QUALIFIED_GRANDFATHER: frozenset[str] = frozenset(
    {
        # R4 discriminated-arg install exception (single "install", not source-split)
        "plugin_management__install",
        # single-entry-category discriminated install, predates source-split;
        # NOT an X__X violation (category "presentation_management" != verb
        # "install") but the bare "install" verb itself is grandfathered
        "presentation_management__install",
        # object_verb order anomaly, mirrors the flat "index_update" anomaly
        "rag_operation__index_update",
        # term-of-art compound, mirrors the flat "semantic_search" anomaly
        "rag_operation__semantic_search",
    }
)


def _classify_flat(name: str) -> tuple[bool, str]:
    """Return (compliant, reason) for a flat registry name."""
    if name in FLAT_GRANDFATHER:
        return True, "grandfathered"
    if name in CANONICAL_VERBS:
        return True, "bare canonical verb (no object needed)"
    for compound in _COMPOUND_VERBS:
        if name == compound or name.startswith(compound + "_"):
            return True, f"compound verb {compound!r}"
    first_token = name.split("_", 1)[0]
    if first_token in CANONICAL_VERBS:
        return True, f"verb_object (verb={first_token!r})"
    return False, f"first token {first_token!r} not in canonical verb lexicon"


def _classify_qualified(name: str) -> tuple[bool, str]:
    """Return (compliant, reason) for a catalog qualified name."""
    if name in QUALIFIED_GRANDFATHER:
        return True, "grandfathered"
    if "__" not in name:
        return False, "missing '__' category/verb separator"
    category, remainder = name.split("__", 1)
    if category == remainder:
        return False, f"R5 violation: X__X (category == verb == {category!r})"
    for compound in _COMPOUND_VERBS:
        if remainder == compound or remainder.startswith(compound + "_"):
            return True, f"compound verb {compound!r}"
    first_token = remainder.split("_", 1)[0]
    if first_token in CANONICAL_VERBS:
        return True, f"verb={first_token!r}"
    return False, f"verb token {first_token!r} (from {remainder!r}) not in canonical verb lexicon"


def _live_flat_names() -> list[str]:
    from reyn.tools import get_default_registry

    return sorted(get_default_registry().names())


def _live_qualified_names() -> list[str]:
    from reyn.tools.universal_dispatch import _OPERATION_RULES

    return sorted(_OPERATION_RULES.keys())


# ---------------------------------------------------------------------------
# Vacuity guards (mandatory per architect §D) — an empty enumeration must not
# silently pass, and an empty grandfather set must not silently fail-everything.
# ---------------------------------------------------------------------------


def test_flat_registry_enumeration_is_nonempty() -> None:
    """Tier 2: vacuity guard — a broken enumeration (wrong import, empty
    registry at test time) must fail loudly here, not silently pass the
    coverage assertion below over an empty set.

    Deliberately NOT a ``len(...) >= N`` size pin (Tier 4 format-pinning per
    ``test_tier_audit.py``, the #3193 precedent's own stated reason for
    avoiding it) — instead, sentinel membership: a handful of tool names
    KNOWN to live in different subsystems (file ops, MCP, memory, cron), so
    a bug that only breaks one code path can't accidentally satisfy all of
    them."""
    names = set(_live_flat_names())
    sentinels = {"read_file", "list_mcp_servers", "forget_memory", "cron_list"}
    missing = sentinels - names
    assert not missing, (
        f"expected sentinel flat tool name(s) {sorted(missing)} were NOT found "
        f"by get_default_registry().names() (found {len(names)} names total) — "
        f"the registry very likely failed to populate at test time, not that "
        f"these tools were actually removed"
    )


def test_qualified_registry_enumeration_is_nonempty() -> None:
    """Tier 2: vacuity guard for the catalog-qualified enumeration — same
    sentinel-membership idiom as the flat guard above, not a size pin."""
    names = set(_live_qualified_names())
    sentinels = {"file__read", "mcp__list_servers", "memory_operation__forget"}
    missing = sentinels - names
    assert not missing, (
        f"expected sentinel qualified name(s) {sorted(missing)} were NOT found "
        f"in universal_dispatch._OPERATION_RULES (found {len(names)} names "
        f"total) — the enumeration very likely failed, not that these tools "
        f"were actually removed"
    )


def test_grandfather_sets_are_nonempty() -> None:
    """Tier 2: vacuity guard — an empty grandfather set would make every
    single pre-existing non-conforming name fail, which is not this gate's
    job (owner policy: no rename sweep, grandfather what predates the
    convention)."""
    assert FLAT_GRANDFATHER, "FLAT_GRANDFATHER must not be empty"
    assert QUALIFIED_GRANDFATHER, "QUALIFIED_GRANDFATHER must not be empty"


# ---------------------------------------------------------------------------
# The gate itself
# ---------------------------------------------------------------------------


def test_every_flat_tool_name_fits_convention_or_is_grandfathered() -> None:
    """Tier 2: OS-invariant — every registered flat tool name either fits
    the R1 (word-order) / R2 (frozen removal-class) / R4 (source-split
    install) structural convention, or is in the frozen grandfather
    allowlist with a documented reason. A newly added tool that fits
    neither is naming drift this gate exists to catch (#3223)."""
    failures = []
    for name in _live_flat_names():
        ok, reason = _classify_flat(name)
        if not ok:
            failures.append(f"{name}: {reason}")
    assert not failures, (
        "the following flat tool name(s) do not fit the tool-naming convention "
        "in docs/reference/runtime/tool-naming.md and are not in FLAT_GRANDFATHER: "
        + "; ".join(failures)
        + " — either rename to fit the convention, or add to FLAT_GRANDFATHER "
        "with a reason (only if this predates the convention intentionally)"
    )


def test_every_qualified_tool_name_fits_convention_or_is_grandfathered() -> None:
    """Tier 2: OS-invariant — every catalog-qualified name either fits the
    `category__verb` structural convention (including R5's no-X__X rule) or
    is in the frozen grandfather allowlist with a documented reason."""
    failures = []
    for name in _live_qualified_names():
        ok, reason = _classify_qualified(name)
        if not ok:
            failures.append(f"{name}: {reason}")
    assert not failures, (
        "the following qualified tool name(s) do not fit the tool-naming "
        "convention in docs/reference/runtime/tool-naming.md and are not in "
        "QUALIFIED_GRANDFATHER: " + "; ".join(failures) + " — either rename to "
        "fit the convention, or add to QUALIFIED_GRANDFATHER with a reason"
    )


def test_removal_verbs_lexicon_is_exactly_the_frozen_four() -> None:
    """Tier 2: contract — REMOVAL_VERBS (R2's frozen class table) must be
    exactly {delete, drop, forget, uninstall}, and every one of those four
    must be present in CANONICAL_VERBS (otherwise a real registered removal
    name using one of them would wrongly fail the main gate). Guards
    against someone editing REMOVAL_VERBS and CANONICAL_VERBS out of sync,
    which the tautological alternative (checking seen-verbs subset-of
    REMOVAL_VERBS, derived FROM CANONICAL_VERBS membership) cannot catch."""
    assert REMOVAL_VERBS == frozenset({"delete", "drop", "forget", "uninstall"}), (
        "R2's removal class table drifted from the frozen 4-verb set — see "
        "docs/reference/runtime/tool-naming.md § R2 before adding a 5th verb"
    )
    assert REMOVAL_VERBS <= CANONICAL_VERBS, (
        "every R2 removal verb must also be a member of CANONICAL_VERBS, or "
        "real registered removal-class tool names would fail the main gate"
    )


# ---------------------------------------------------------------------------
# FP-both-sides test (architect co-vet criterion, §D/§3)
# ---------------------------------------------------------------------------


def test_synthetic_compliant_new_name_passes() -> None:
    """Tier 2: FP-guard (compliant side) — a synthetic NEW tool name that
    correctly follows the convention must pass, proving the gate is not
    accidentally rejecting valid new names."""
    ok, _ = _classify_flat("read_widget")
    assert ok, "a convention-compliant new flat name (verb_object, canonical verb) must pass"

    ok, _ = _classify_qualified("widget_management__read")
    assert ok, "a convention-compliant new qualified name (category__verb) must pass"


def test_synthetic_noncompliant_new_name_is_rejected() -> None:
    """Tier 2: FP-guard (non-compliant side) — a synthetic NEW tool name
    that violates the convention must be rejected, proving the gate is
    load-bearing rather than vacuously accepting everything.

    Two distinct violation shapes, per architect's co-vet examples:
    (1) a 5th removal verb (`delete_widget` reads as removal-class but
        exercises no verb outside the frozen four, so use an actual 5th
        verb `remove_widget` to prove the removal-class closure), and
    (2) an X__X qualified name (`widget__widget`)."""
    ok, reason = _classify_flat("remove_widget")
    assert not ok, f"'remove_widget' (5th removal verb 'remove') should be REJECTED, got: {reason}"

    ok, reason = _classify_qualified("widget__widget")
    assert not ok, f"'widget__widget' (R5 X__X violation) should be REJECTED, got: {reason}"


# ---------------------------------------------------------------------------
# Load-bearing self-documentation (not itself part of the gate — a permanent
# record of the strip-grandfather sanity check performed during review, so a
# future reader doesn't have to re-derive it from scratch).
# ---------------------------------------------------------------------------


def test_stripping_grandfather_makes_real_names_fail() -> None:
    """Tier 2: load-bearing proof — if FLAT_GRANDFATHER/QUALIFIED_GRANDFATHER
    were empty, real currently-registered tool names would fail the
    convention check. This proves the gate actually constrains something
    (it is not vacuously green regardless of what the grandfather set
    contains). Does not modify the module-level frozensets (uses local
    empty sets), so it cannot corrupt the real gate's state for other tests
    in this file."""
    empty_flat: frozenset[str] = frozenset()
    empty_qualified: frozenset[str] = frozenset()

    def classify_flat_stripped(name: str) -> bool:
        if name in empty_flat:
            return True
        if name in CANONICAL_VERBS:
            return True
        for compound in _COMPOUND_VERBS:
            if name == compound or name.startswith(compound + "_"):
                return True
        return name.split("_", 1)[0] in CANONICAL_VERBS

    def classify_qualified_stripped(name: str) -> bool:
        if name in empty_qualified:
            return True
        if "__" not in name:
            return False
        category, remainder = name.split("__", 1)
        if category == remainder:
            return False
        for compound in _COMPOUND_VERBS:
            if remainder == compound or remainder.startswith(compound + "_"):
                return True
        return remainder.split("_", 1)[0] in CANONICAL_VERBS

    flat_failures = [n for n in _live_flat_names() if not classify_flat_stripped(n)]
    qualified_failures = [n for n in _live_qualified_names() if not classify_qualified_stripped(n)]

    # At authoring time: 35 flat + 4 qualified real names go RED with the
    # grandfather set stripped (e.g. agent_spawn, cron_disable, mcp_call_tool,
    # plugin_management__install, rag_operation__index_update). We assert a
    # generous lower bound rather than pinning the exact count (which would
    # be a Tier-4 format pin) — the property under test is "load-bearing",
    # i.e. strictly greater than zero.
    assert len(flat_failures) > 0, (
        "expected at least one real flat tool name to fail WITHOUT the "
        "grandfather allowlist (proves the gate is load-bearing, not "
        "vacuous) — got zero, meaning CANONICAL_VERBS alone already covers "
        "every registered name and the grandfather set constrains nothing"
    )
    assert len(qualified_failures) > 0, (
        "expected at least one real qualified tool name to fail WITHOUT the "
        "grandfather allowlist — got zero, meaning the gate is vacuous for "
        "qualified names"
    )
