"""Tier 2: #5093 — scripts/check_remote_snapshot_placeholder_declared.py.

Architect ruling (issuecomment-5384873023): a graceful-degrade placeholder
in ``project_remote_snapshot``'s return dict must have a declared axis
(``ChatReadModelCapabilities``), a ``_WIRE_KEYS`` membership, or a cited
non-fabricating exemption — never a bare hand-typed literal a producer can
silently forget to update. Real ``project_remote_snapshot``/
``ChatReadModelCapabilities``/``_WIRE_KEYS`` for acceptance①; synthetic
fixture files (mirrors ``test_5131_tui_widget_boundary.py``'s own pattern)
for the falsification witnesses — a gate that only confirms green on the
ALREADY-COMPLIANT current tree cannot distinguish "enforcing" from
"never runs at all" (CLAUDE.md's own test-review question 4).

``TestWireKeysBackedByProjectStatus`` (below) is the follow-up architect
blocking finding (issuecomment-5385179961, PR #5206 A #1): ``_WIRE_KEYS``
was a hand-typed ASSERTION with no producer code reading it — nothing
checked that its members were genuinely, unconditionally on the wire.
``find_wire_keys_violations`` closes that by AST-checking ``_WIRE_KEYS ⊆``
the keys ``agui/state.py``'s ``project_status`` unconditionally emits.

``TestUnwiredKeyViolations`` (below) is #5771 stage ① (lead-coder dispatch,
owner ruling "structural, not a feature"): a THIRD, independent question
neither check above asks — is a ``project_remote_snapshot`` OUTPUT key
itself backed by a same-named ``project_status`` key at all, regardless of
whether its placeholder-ness is otherwise honestly declared.
``find_unwired_key_violations`` closes that. Unlike the other 2 checks,
this one is NOT asserted to be ``== []`` on the real source — #5098's own
invariant ("one declaration, not two that can drift apart") is measurably
broken today, for real, disclosed keys (``cost_usd``/``usage``/
``session_cached_tokens`` are the 3 lead-coder's own dispatch named as the
required witnesses; the rest are disclosed here per CLAUDE.md's test-review
question 4, not silenced by an allowlist invented for this PR)."""
from __future__ import annotations

from pathlib import Path

from scripts.check_remote_snapshot_placeholder_declared import (
    count_examined_output_keys,
    find_new_unwired_key_violations,
    find_unwired_key_violations,
    find_violations,
    find_wire_keys_violations,
)

# ── acceptance① — the real source, right now, has zero violations ────────


def test_the_real_source_has_zero_violations() -> None:
    """Tier 2: acceptance① — landed on main, every placeholder-shaped key
    in the real ``project_remote_snapshot`` is covered by one of the 3
    remedies. Any hit here is a real regression, not inherited debt."""
    violations = find_violations()
    assert violations == [], f"real regression(s) found: {violations}"


# ── acceptance② — a new undeclared placeholder key is flagged ────────────


def _write_module(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "fixture_read_model.py"
    path.write_text(
        "def project_remote_snapshot(values):\n"
        "    v = values or {}\n"
        f"{body}\n",
        encoding="utf-8",
    )
    return path


def test_a_bare_literal_placeholder_with_no_remedy_is_flagged(tmp_path: Path) -> None:
    """Tier 2: acceptance② shape (a) — a bare ``[]`` literal for a key with
    no matching ``ChatReadModelCapabilities`` field, no ``_WIRE_KEYS``
    membership, and no cited exemption. The exact PRE-#5097 shape witness①
    in #5093's own issue thread describes."""
    path = _write_module(
        tmp_path,
        '    return {"totally_new_placeholder_key": []}\n',
    )

    violations = find_violations(path)

    assert any("totally_new_placeholder_key" in v for v in violations), violations


def test_a_get_call_placeholder_with_no_remedy_is_flagged(tmp_path: Path) -> None:
    """Tier 2: acceptance② shape (b) — ``v.get(key, [])`` for an
    undeclared key. This is the shape lead-coder's corrected placeholder
    definition (PR #5097 review) exists to still catch even AFTER a bare
    literal is moved to a ``.get`` call — the wire-read shape alone does
    not prove the key can never be absent."""
    path = _write_module(
        tmp_path,
        '    return {"another_new_key": v.get("another_new_key", [])}\n',
    )

    violations = find_violations(path)

    assert any("another_new_key" in v for v in violations), violations


def test_a_get_call_with_no_default_is_not_flagged(tmp_path: Path) -> None:
    """Tier 2: falsification contrast — ``v.get(key)`` with NO default arg
    (like real ``attached_name``/``pending_intervention_head``'s bare
    reads) is not a placeholder shape at all; must never be flagged."""
    path = _write_module(
        tmp_path,
        '    return {"bare_optional_key": v.get("bare_optional_key")}\n',
    )

    assert find_violations(path) == []


def test_a_non_placeholder_value_is_not_flagged(tmp_path: Path) -> None:
    """Tier 2: falsification contrast — a non-empty string constant (a
    genuine label, not a degrade placeholder) must never be flagged."""
    path = _write_module(
        tmp_path,
        '    return {"a_real_label": "some-real-value"}\n',
    )

    assert find_violations(path) == []


# ── acceptance③ — declaring the key (any of the 3 remedies) clears it ────


def test_declaring_a_wire_keys_member_clears_the_flag(tmp_path: Path) -> None:
    """Tier 2: acceptance③ remedy① — a key whose ``.get()`` call reads a
    ``_WIRE_KEYS`` member is exempt. Uses a REAL ``_WIRE_KEYS`` entry
    (``cost_agent``) rather than a synthetic one, since that set is
    imported from the real module, not parameterizable per-test."""
    path = _write_module(
        tmp_path,
        '    return {"cost_agent_alias": v.get("cost_agent", 0.0)}\n',
    )

    assert find_violations(path) == []


def test_declaring_a_direct_suffix_axis_clears_the_flag(tmp_path: Path) -> None:
    """Tier 2: acceptance③ remedy② (direct suffix match) — a key whose
    name, with ``_reported`` appended, matches a real
    ``ChatReadModelCapabilities`` field (``hooks_reported``)."""
    path = _write_module(
        tmp_path,
        '    return {"hooks": []}\n',
    )

    assert find_violations(path) == []


def test_an_undeclared_key_sharing_a_name_with_a_cleared_key_is_not_flagged(
    tmp_path: Path,
) -> None:
    """Tier 2: acceptance③ remedy③ — a key in the real
    ``_CLEARED_NON_FABRICATING_KEYS`` set (``skills``) is exempt without
    needing its own axis."""
    path = _write_module(
        tmp_path,
        '    return {"skills": []}\n',
    )

    assert find_violations(path) == []


# ── strip-falsify — the gate script itself, as a subprocess ──────────────


def test_main_exits_nonzero_when_a_violation_exists(tmp_path: Path, monkeypatch) -> None:
    """Tier 2: the CLI entry point (not just the underlying detector) must
    fail loudly — mirrors the other 2026-08-23 gate test files' own
    main()-level witness (test_5131_tui_widget_boundary.py's own
    rationale: a detector-only test can pass while main() itself is wired
    wrong, e.g. an exit-code inversion)."""
    import scripts.check_remote_snapshot_placeholder_declared as gate_module

    fixture = _write_module(tmp_path, '    return {"totally_undeclared": []}\n')
    monkeypatch.setattr(gate_module, "_PACKAGE_DIR", fixture)

    exit_code = gate_module.main()

    assert exit_code == 1


def test_main_exits_nonzero_when_only_an_unwired_key_violation_exists(
    tmp_path: Path, monkeypatch,
) -> None:
    """Tier 2: main()-level witness for #5771's own axis specifically —
    isolates it from the other 2 checks (a fixture with a REAL wire key
    read via ``.get()``, so ``find_violations`` sees nothing placeholder-
    shaped to flag, paired with a synthetic ``project_status`` that omits
    that key) to prove ``main()`` fails on THIS check's finding alone, not
    only when one of the other 2 also happens to fire on the same
    fixture (as ``test_main_exits_nonzero_when_a_violation_exists`` above
    would, since its own fixture key is unwired too).

    Uses a key name NOT in ``_UNWIRED_KEY_VIOLATIONS_BASELINE`` (unlike
    ``cron_jobs``, a real, currently-baselined LOCAL entry) —
    ``main()``'s own exit code is gated on ``find_new_unwired_key_
    violations`` specifically, so a baselined violation alone must NOT
    fail it; only a genuinely NEW one does."""
    import scripts.check_remote_snapshot_placeholder_declared as gate_module

    remote_fixture = _write_module(
        tmp_path, '    return {"brand_new_unbaselined_key": v.get("cost_agent")}\n',
    )
    # Every real _WIRE_KEYS member, so find_wire_keys_violations (a
    # DIFFERENT check, module-level _WIRE_KEYS unaffected by this fixture)
    # stays clean and this test isolates #5771's own axis alone.
    status_fixture = _write_status_module(
        tmp_path,
        ["model", "cost_agent", "cost_total", "cost_usd", "agent_tokens",
         "ctx_used", "ctx_window", "queue", "turn_active", "queue_seq",
         "cost_breakdown_session", "cost_breakdown_agent", "cost_breakdown_project",
         "usage", "session_cached_tokens", "turn_cost_usd", "turn_tokens",
         "mcp_probe_states", "hooks_config_warnings", "ctx_recent_usage"],
    )
    monkeypatch.setattr(gate_module, "_PACKAGE_DIR", remote_fixture)
    monkeypatch.setattr(gate_module, "_AGUI_STATE_PATH", status_fixture)

    assert gate_module.find_violations(remote_fixture) == []
    assert gate_module.find_wire_keys_violations(status_fixture) == []

    exit_code = gate_module.main()

    assert exit_code == 1


# ── _WIRE_KEYS ⊆ project_status's unconditional keys (architect blocking
# finding, issuecomment-5385179961) ───────────────────────────────────────


def _write_status_module(tmp_path: Path, keys: "list[str]") -> Path:
    path = tmp_path / "fixture_agui_state.py"
    body_lines = "\n".join(f'        "{k}": snap.get("{k}"),' for k in keys)
    path.write_text(
        "def project_status(snapshot, *, waiting_on=None):\n"
        "    snap = snapshot or {}\n"
        "    out = {\n"
        f"{body_lines}\n"
        "    }\n"
        "    return out\n",
        encoding="utf-8",
    )
    return path


def test_the_real_wire_keys_are_a_verified_subset_of_project_status() -> None:
    """Tier 2: acceptance① — landed on main, every ``_WIRE_KEYS`` member is
    genuinely one of ``project_status``'s own unconditionally-emitted keys."""
    violations = find_wire_keys_violations()
    assert violations == [], f"real regression(s) found: {violations}"


def test_a_wire_key_dropped_from_project_status_is_flagged(tmp_path: Path) -> None:
    """Tier 2: acceptance② — the exact architect finding: a key present in
    ``_WIRE_KEYS`` but no longer emitted by ``project_status`` (e.g. removed
    from the wire protocol) must be flagged, not silently trusted. Uses a
    REAL ``_WIRE_KEYS`` member (``cost_agent``) with a synthetic
    ``project_status`` fixture that omits it -- the exact "wire protocol
    changed but the hand-typed assertion didn't move" shape."""
    fixture = _write_status_module(
        tmp_path,
        # every real _WIRE_KEYS member EXCEPT "cost_agent"
        ["cost_total", "agent_tokens", "ctx_used", "ctx_window", "queue",
         "turn_active", "queue_seq"],
    )

    violations = find_wire_keys_violations(fixture)

    assert any("cost_agent" in v for v in violations), violations


def test_project_status_emitting_every_wire_key_is_not_flagged(tmp_path: Path) -> None:
    """Tier 2: falsification contrast — a fixture that DOES emit every real
    ``_WIRE_KEYS`` member (plus an unrelated extra key, proving the check is
    a subset test, not an exact-set one) produces zero violations."""
    fixture = _write_status_module(
        tmp_path,
        ["cost_agent", "cost_total", "cost_usd", "agent_tokens", "ctx_used",
         "ctx_window", "queue", "turn_active", "queue_seq",
         "cost_breakdown_session", "cost_breakdown_agent", "cost_breakdown_project",
         "usage", "session_cached_tokens", "turn_cost_usd", "turn_tokens",
         "mcp_probe_states", "hooks_config_warnings", "ctx_recent_usage",
         "some_unrelated_extra_key"],
    )

    assert find_wire_keys_violations(fixture) == []


# ── #5771 stage ① — is a project_remote_snapshot output key itself backed
# by a same-named project_status key, independent of ①②③ above ──────────


class TestUnwiredKeyViolations:
    # #5771 stage②: test_the_real_source_exposes_the_known_cost_tab_drift
    # (the 3-witness test: cost_usd/usage/session_cached_tokens must
    # appear in find_unwired_key_violations's real-source output) is
    # DELETED here, exactly per its own documented fate — stage② just
    # wired all 3 for real, so they no longer appear in that output at
    # all. Its non-vacuity role was never this test's job in the first
    # place (see TestCountExaminedOutputKeys, which never expires).

    def test_an_output_key_with_no_matching_project_status_key_is_flagged(
        self, tmp_path: Path,
    ) -> None:
        """Tier 2: acceptance② — a synthetic ``project_remote_snapshot``
        mapping a key ``project_status`` never emits must be flagged,
        independent of the value's own shape (a bare literal here, the
        simplest case)."""
        remote_fixture = _write_module(tmp_path, '    return {"not_on_the_wire": 0}\n')
        status_fixture = _write_status_module(tmp_path, ["model", "cost_agent"])

        violations = find_unwired_key_violations(remote_fixture, status_fixture)

        assert any('"not_on_the_wire"' in v for v in violations), violations

    def test_an_alias_reading_a_different_real_wire_key_is_still_flagged(
        self, tmp_path: Path,
    ) -> None:
        """Tier 2: acceptance② (the exact #5771 root cause) — an output key
        whose value reads a DIFFERENT, genuinely-real wire key under a
        mismatched name (``cost_usd``'s own real shape:
        ``v.get("cost_agent", 0.0)``) is flagged by its OWN name, not
        excused by the wire key it happens to alias. This is precisely what
        #5093's original ``find_violations`` cannot see (remedy① there
        matches on the ``.get()`` call's own key ARGUMENT, "cost_agent",
        not the output key "cost_usd")."""
        remote_fixture = _write_module(
            tmp_path, '    return {"cost_usd": v.get("cost_agent", 0.0)}\n',
        )
        status_fixture = _write_status_module(tmp_path, ["model", "cost_agent"])

        violations = find_unwired_key_violations(remote_fixture, status_fixture)

        assert any('"cost_usd"' in v for v in violations), violations
        # #5093's OWN gate, unmodified, must NOT catch this shape -- it is
        # the exact gap #5771 exists to close, not a duplicate of it.
        assert find_violations(remote_fixture) == []

    def test_an_output_key_backed_by_a_same_named_project_status_key_is_not_flagged(
        self, tmp_path: Path,
    ) -> None:
        """Tier 2: falsification contrast — an output key whose name
        genuinely matches a real ``project_status`` key produces zero
        violations for that key, proving this is a same-name membership
        test, not something that flags every key unconditionally."""
        remote_fixture = _write_module(
            tmp_path, '    return {"cost_agent": v.get("cost_agent", 0.0)}\n',
        )
        status_fixture = _write_status_module(tmp_path, ["model", "cost_agent"])

        assert find_unwired_key_violations(remote_fixture, status_fixture) == []

    def test_the_reported_snapshot_keys_spread_is_expanded_not_silently_skipped(
        self, tmp_path: Path,
    ) -> None:
        """Tier 2: architect BLOCKING finding (#5773, agreed by lead-coder)
        — a ``**spread`` entry used to be skipped unconditionally
        (``if key_node is None: continue``), making the ENTIRE
        ``ChatReadModelCapabilities`` field family invisible to this
        census (project_status has zero ``*_reported``-style keys, so
        every one of those 21 fields is unwired drift the OLD code never
        reported). The real
        ``**reported_snapshot_keys(REMOTE_CHAT_READ_CAPABILITIES)`` shape
        is now recognized and resolved via a genuine import + call, so its
        real field names are checked the same as any literal key."""
        remote_fixture = _write_module(
            tmp_path, '    return {**reported_snapshot_keys(REMOTE_CHAT_READ_CAPABILITIES)}\n',
        )
        status_fixture = _write_status_module(tmp_path, ["model"])

        violations = find_unwired_key_violations(remote_fixture, status_fixture)

        assert any(
            '"completion_source"' in v and "via **reported_snapshot_keys" in v
            for v in violations
        ), violations

    def test_an_unrecognized_spread_shape_is_flagged_not_silently_skipped(
        self, tmp_path: Path,
    ) -> None:
        """Tier 2: acceptance② — a ``**spread`` this check does NOT
        recognize (some future/hypothetical helper, not
        ``reported_snapshot_keys(REMOTE_CHAT_READ_CAPABILITIES)``) must
        still surface as a violation demanding manual review, never a
        silent pass — the exact "skip means the gate can be blind without
        saying so" shape the architect finding closed."""
        remote_fixture = _write_module(
            tmp_path, '    return {**some_other_helper()}\n',
        )
        status_fixture = _write_status_module(tmp_path, ["model"])

        violations = find_unwired_key_violations(remote_fixture, status_fixture)

        assert any("unrecognized `**spread`" in v for v in violations), violations


# ── #5773 — _find_function_return_dict must not silently pick the wrong
# (or an empty) return when a producer gains an early guard-clause return ──


class TestFindFunctionReturnDictGuards:
    def test_multiple_top_level_returns_are_refused_not_guessed(
        self, tmp_path: Path,
    ) -> None:
        """Tier 2: architect BLOCKING finding (#5773) — an early guard-
        clause return (``if values is None: return {}``) ahead of the
        real return used to be silently picked or silently ignored
        depending on ``ast.walk``'s own traversal order; this must refuse
        instead, since guessing which return is "the real one" can
        silently shrink this gate's own population to whatever the guard
        clause happens to return."""
        path = _write_module(
            tmp_path,
            '    if values is None:\n'
            '        return {}\n'
            '    return {"cost_agent": v.get("cost_agent", 0.0)}\n',
        )
        status_fixture = _write_status_module(tmp_path, ["model"])

        violations = find_unwired_key_violations(path, status_fixture)

        assert any("return statements" in v for v in violations), violations

    def test_an_empty_dict_return_is_refused_not_treated_as_a_clean_population(
        self, tmp_path: Path,
    ) -> None:
        """Tier 2: architect BLOCKING finding (#5773) — a resolved return
        dict with ZERO keys must be refused as a mis-resolution, never
        silently accepted as "a clean, fully-compliant population" (the
        exact empty-population-still-green shape CLAUDE.md's own
        test-review question 4 names)."""
        path = _write_module(tmp_path, '    return {}\n')
        status_fixture = _write_status_module(tmp_path, ["model"])

        violations = find_unwired_key_violations(path, status_fixture)

        assert any("EMPTY dict" in v for v in violations), violations


# ── #5771 — the ratchet: real-source violations must be a SUBSET of the
# baseline; a genuinely NEW one is red, no matter how many baselined ones
# already exist ─────────────────────────────────────────────────────────


class TestNewUnwiredKeyViolationsRatchet:
    def test_the_real_source_has_no_new_unwired_key_violations(self) -> None:
        """Tier 2: acceptance① — every unwired-key violation on the real
        tree is already accounted for in ``_UNWIRED_KEY_VIOLATIONS_
        BASELINE``. This is the actual merge-blocking ratchet (lead-coder
        BLOCKING, PR #5773): stage②'s own 8 new wire keys can freely be
        ADDED without this test needing to change, but a key that stays
        unwired by ACCIDENT — the exact "nothing stops a 41st drifted key"
        gap the BLOCKING named — fails here immediately."""
        assert find_new_unwired_key_violations() == []

    def test_a_key_outside_the_baseline_is_flagged_as_new(
        self, tmp_path: Path,
    ) -> None:
        """Tier 2: acceptance② — a synthetic unwired key with a name that
        does not appear anywhere in the real baseline is flagged by
        ``find_new_unwired_key_violations``, proving the subset check
        actually fires rather than accepting everything unconditionally."""
        remote_fixture = _write_module(
            tmp_path, '    return {"a_key_nobody_has_ever_seen_before": 0}\n',
        )
        status_fixture = _write_status_module(tmp_path, ["model"])

        violations = find_new_unwired_key_violations(remote_fixture, status_fixture)

        assert any(
            '"a_key_nobody_has_ever_seen_before"' in v for v in violations
        ), violations

    def test_a_baselined_key_alone_is_not_flagged_as_new(
        self, tmp_path: Path,
    ) -> None:
        """Tier 2: falsification contrast — a real, already-baselined key
        (``cron_jobs``, disposition LOCAL) alone produces zero NEW
        violations, proving the ratchet distinguishes disclosed debt from
        genuinely new drift rather than flagging every unwired key."""
        remote_fixture = _write_module(tmp_path, '    return {"cron_jobs": []}\n')
        status_fixture = _write_status_module(tmp_path, ["model"])

        assert find_new_unwired_key_violations(remote_fixture, status_fixture) == []


# ── #5771 — a non-expiring non-vacuity witness: stage② fixing the 3
# required cost-tab keys must not silently remove the ONLY thing proving
# this walk still examines something real ───────────────────────────────


class TestCountExaminedOutputKeys:
    def test_the_real_source_examines_more_than_zero_output_keys(self) -> None:
        """Tier 2: the non-expiring non-vacuity guard (lead-coder
        BLOCKING, PR #5773): asserting specific KEYS are still violations
        (the original version of this PR's own witness) expires the
        moment those keys are fixed — a silently-broken walk and a
        genuinely-clean population would then look identical (0
        violations either way). Counting EXAMINED keys instead of
        VIOLATIONS never expires: ``project_remote_snapshot`` will always
        have SOME output keys, so this stays true forever unless the walk
        itself regresses."""
        assert count_examined_output_keys() > 0

    def test_a_return_with_only_wired_keys_still_counts_them(
        self, tmp_path: Path,
    ) -> None:
        """Tier 2: falsification contrast — a fixture with zero
        violations (every key genuinely wired) still reports a nonzero
        examined-key count, proving this witness measures "did the walk
        see keys at all", not "how many violations exist"."""
        remote_fixture = _write_module(
            tmp_path, '    return {"cost_agent": v.get("cost_agent", 0.0)}\n',
        )

        assert count_examined_output_keys(remote_fixture) == 1

    def test_an_empty_return_reports_zero_examined_keys(
        self, tmp_path: Path,
    ) -> None:
        """Tier 2: falsification contrast — the exact regressed-walk shape
        this witness exists to catch: an empty return dict (refused
        elsewhere as a mis-resolution, see ``TestFindFunctionReturnDict
        Guards``) reports 0 examined keys, proving this witness would
        actually go red if the real walk ever silently found nothing."""
        remote_fixture = _write_module(tmp_path, '    return {}\n')

        assert count_examined_output_keys(remote_fixture) == 0
