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
"""
from __future__ import annotations

from pathlib import Path

from scripts.check_remote_snapshot_placeholder_declared import (
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
        ["cost_agent", "cost_total", "agent_tokens", "ctx_used", "ctx_window",
         "queue", "turn_active", "queue_seq", "some_unrelated_extra_key"],
    )

    assert find_wire_keys_violations(fixture) == []
