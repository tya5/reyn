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
# get_default_registry().names() and
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
        "register", "unregister", "enable", "disable",
        # #4004: added for create_topology (renamed from topology_create) —
        # owner-ratified rename sweep, see docs/reference/runtime/tool-naming.md
        # § "family-prefix validity condition".
        "create",
    }
)

# Lifecycle-toggle dual-pair verbs (register<->unregister, enable<->disable).
# These are ALSO accepted in OBJECT_verb suffix position (not just the R1
# verb_object prefix position all other CANONICAL_VERBS get), because the
# existing cron_* family already uses them that way and a future cron-like
# family legitimately may do the same (R1: "family-internal consistency >
# global rule"). Scoped to exactly these four verbs — NOT a blanket
# first-OR-last-token rule for every canonical verb, which would silently
# sweep unrelated grandfather entries (web_fetch, semantic_search, ...) into
# "structurally compliant" for reasons that have nothing to do with this
# lifecycle-toggle class.
LIFECYCLE_TOGGLE_VERBS: frozenset[str] = frozenset({"register", "unregister", "enable", "disable"})

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
        # #4004: agent_spawn/session_spawn renamed to spawn_agent/spawn_session
        # (owner-ratified rename sweep) — both are now compliant verb_object
        # names ("spawn" is already in CANONICAL_VERBS), no longer grandfathered.
        # cron_* family (R1 family-prefix grandfather). NOTE: cron_disable /
        # cron_enable / cron_register / cron_unregister are NOT here — they
        # now pass structurally via LIFECYCLE_TOGGLE_VERBS suffix matching
        # (see _classify_flat). cron_list stays grandfathered: "list" is a
        # verb_object-position verb elsewhere in the lexicon, not a
        # lifecycle-toggle verb, so cron_list's object_verb order still
        # needs the family-prefix grandfather exemption.
        "cron_list",
        # R3 sole pre-existing `get` exception (new `get` is forbidden)
        "get_mcp_prompt",
        # sole hooks_*-shaped tool, predates convention, no established family
        "hooks_add",
        # FP-0066 P1b: "index_update" (object_verb order anomaly) retired along
        # with the layer-1 agent tool it named.
        # mcp_* family (R1 family-prefix grandfather); mcp_install is additionally
        # an orphan superseded by the install_local/_package/_registry split
        # (2026-05-25) and not a catalog action any more — flagged
        # as dead surface, candidate for a separate retire follow-up
        "mcp_call_tool", "mcp_drop_server", "mcp_install", "mcp_install_local",
        "mcp_install_package", "mcp_install_registry", "mcp_search_registry",
        # pipeline_* family (R1 family-prefix grandfather)
        "pipeline_install_local", "pipeline_install_source", "pipeline_list",
        # plugin_* trio: R4 grandfather for the single discriminated-arg
        # "install" (not source-split). #3429 renamed these off the
        # catalog-style ``plugin_management__*`` spelling onto R1's
        # verb_object default, so their word ORDER is now compliant and only
        # the bare "install"/"uninstall" verb needs the R4 exemption.
        "install_plugin", "list_plugins",
        "uninstall_plugin",
        # sole presentation_*-shaped install, predates the source-split convention
        "presentation_install_local",
        # reyn_repo_* family (R1 family-prefix grandfather)
        "reyn_repo_glob", "reyn_repo_grep", "reyn_repo_list", "reyn_repo_read",
        # FP-0066 P1b: "semantic_search" (established term-of-art compound)
        # retired along with the layer-1 agent tool it named.
        # skill_* family (R1 family-prefix grandfather)
        "skill_install_local", "skill_install_source", "skill_list",
        # #4004: topology_create renamed to create_topology (owner-ratified
        # rename sweep) — now a compliant verb_object name using the newly
        # added "create" canonical verb, no longer an anomaly grandfather.
        # web_* family (R1 family-prefix grandfather), object_verb order
        "web_fetch", "web_search",
    }
)

# #3429 deleted the QUALIFIED half of this gate. There was a second namespace
# of ``<category>__<verb>`` catalog names with its own structural rules (R5's
# "no X__X"), its own grandfather set, and its own classifier; the namespace is
# gone, so the rules that governed it are too. The property that replaces them
# — that no name anywhere carries the separator — is
# ``tests/test_no_qualified_tool_names_3429.py``, which derives its subject
# from the registry rather than from a curated list.


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
    last_token = name.rsplit("_", 1)[-1]
    if last_token in LIFECYCLE_TOGGLE_VERBS:
        return True, f"object_verb lifecycle-toggle (verb={last_token!r})"
    return False, f"first token {first_token!r} not in canonical verb lexicon"


def _live_flat_names() -> list[str]:
    from reyn.tools import get_default_registry

    return sorted(get_default_registry().names())


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


def test_grandfather_sets_are_nonempty() -> None:
    """Tier 2: vacuity guard — an empty grandfather set would make every
    single pre-existing non-conforming name fail, which is not this gate's
    job (owner policy: no rename sweep, grandfather what predates the
    convention)."""
    assert FLAT_GRANDFATHER, "FLAT_GRANDFATHER must not be empty"


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


def test_synthetic_noncompliant_new_name_is_rejected() -> None:
    """Tier 2: FP-guard (non-compliant side) — a synthetic NEW tool name
    that violates the convention must be rejected, proving the gate is
    load-bearing rather than vacuously accepting everything.

    The violation shape, per architect's co-vet examples: a 5th removal verb
    (`delete_widget` reads as removal-class but exercises no verb outside the
    frozen four, so use an actual 5th verb `remove_widget` to prove the
    removal-class closure). The second shape this test used to carry — an X__X
    qualified name — went with the qualified namespace (#3429)."""
    ok, reason = _classify_flat("remove_widget")
    assert not ok, f"'remove_widget' (5th removal verb 'remove') should be REJECTED, got: {reason}"


# ---------------------------------------------------------------------------
# Load-bearing self-documentation (not itself part of the gate — a permanent
# record of the strip-grandfather sanity check performed during review, so a
# future reader doesn't have to re-derive it from scratch).
# ---------------------------------------------------------------------------


def test_stripping_grandfather_makes_real_names_fail() -> None:
    """Tier 2: load-bearing proof — if FLAT_GRANDFATHER
    were empty, real currently-registered tool names would fail the
    convention check. This proves the gate actually constrains something
    (it is not vacuously green regardless of what the grandfather set
    contains).

    Calls the REAL ``_classify_flat`` (via a
    monkeypatch of the module-level grandfather global, restored in a
    ``finally``) rather than a hand-duplicated copy of the classification
    logic — a duplicated copy would silently drift out of sync with the
    real classifier (e.g. it would not know about
    ``LIFECYCLE_TOGGLE_VERBS`` suffix-matching unless someone remembered to
    update both places), which is exactly the kind of drift this whole gate
    exists to prevent elsewhere."""
    import sys

    # ``sys.modules[__name__]`` (not ``import tests.test_tool_naming_...``):
    # this test file has no ``tests/__init__.py`` package marker, so pytest
    # imports it under a bare module name — a dotted ``tests.<name>`` import
    # would silently create a SECOND, distinct module object rather than
    # referencing the one pytest already loaded, and mutating that second
    # copy's globals would have zero effect on the real classifier (verified:
    # this is exactly the failure mode that first version of this test hit).
    gate_module = sys.modules[__name__]

    original_flat = gate_module.FLAT_GRANDFATHER
    try:
        gate_module.FLAT_GRANDFATHER = frozenset()
        flat_failures = [n for n in _live_flat_names() if not _classify_flat(n)[0]]
    finally:
        gate_module.FLAT_GRANDFATHER = original_flat

    # Real names go RED with the grandfather set stripped (e.g. agent_spawn,
    # hooks_add, mcp_call_tool, install_plugin — cron_disable/enable/register/
    # unregister no longer need grandfather at all, since they now pass
    # structurally via LIFECYCLE_TOGGLE_VERBS). We assert a lower bound rather
    # than pinning the exact count (which would be a Tier-4 format pin) — the
    # property under test is "load-bearing", i.e. strictly greater than zero.
    assert len(flat_failures) > 0, (
        "expected at least one real flat tool name to fail WITHOUT the "
        "grandfather allowlist (proves the gate is load-bearing, not "
        "vacuous) — got zero, meaning CANONICAL_VERBS alone already covers "
        "every registered name and the grandfather set constrains nothing"
    )


def test_cron_lifecycle_toggle_verbs_pass_via_lexicon_not_grandfather() -> None:
    """Tier 2: contract — cron_register / cron_unregister / cron_enable /
    cron_disable pass the structural check via CANONICAL_VERBS +
    LIFECYCLE_TOGGLE_VERBS suffix-matching, and are correctly ABSENT from
    FLAT_GRANDFATHER (promoting register/unregister/enable/disable to the
    lexicon made their grandfather entries redundant — this pins that they
    were actually removed, not just that the gate happens to pass for some
    other reason)."""
    for name in ("cron_register", "cron_unregister", "cron_enable", "cron_disable"):
        assert name not in FLAT_GRANDFATHER, (
            f"{name!r} should no longer need FLAT_GRANDFATHER now that its verb "
            f"is in LIFECYCLE_TOGGLE_VERBS"
        )
        ok, reason = _classify_flat(name)
        assert ok, f"{name!r} should pass structurally, got: {reason}"
        assert "lifecycle-toggle" in reason, (
            f"{name!r} should pass specifically via the lifecycle-toggle suffix "
            f"path, got reason: {reason!r}"
        )


def test_cron_list_still_needs_grandfather() -> None:
    """Tier 2: contract — cron_list is NOT a lifecycle-toggle verb ("list" is
    a normal verb_object-position verb elsewhere in the lexicon, not a
    register/unregister/enable/disable dual), so it still needs
    FLAT_GRANDFATHER for its object_verb word order. Pins that the
    lifecycle-toggle promotion was scoped narrowly rather than accidentally
    widened into a blanket first-OR-last-token rule for every canonical
    verb (which would sweep unrelated entries like web_fetch or
    semantic_search into "no longer needs grandfather" for reasons that
    have nothing to do with the lifecycle-toggle class)."""
    assert "cron_list" in FLAT_GRANDFATHER, (
        "cron_list should still be grandfathered — 'list' is not a "
        "lifecycle-toggle verb, so it cannot pass via the narrow suffix path"
    )
