"""Tier 2: OS invariant — config doc/example mirror drift guard (#1056 (d)).

`reyn.local.yaml.example` and `docs/reference/config/reyn-yaml.md` are
hand-curated mirrors of the `ReynConfig` schema (the human comments, ordering,
and worked examples are deliberately authored — see #1049/#1053). They drift
the moment a config field is added without a matching mirror edit.

This guard closes that loop in **`--check` mode** (verify, not generate — full
auto-generation would destroy the curated human guidance the schema can't
reproduce): every field the live `walk_config_schema()` advertises must be
documented in BOTH mirrors. It derives the expected field set from the schema
(zero hand-enumeration), so it auto-extends as fields change.

Granularity = **field NAME** (word-boundary), plus every top-level section.
Rationale:
  - Repeated dataclass types (e.g. `CostLimitConfig` appears 9× across
    cost.* / safety.loop.*_per_chain) are documented ONCE as a shape, not 36
    times — name-level coverage matches that good-docs practice, whereas
    dotted-key-precise coverage would force bloated per-instance tables.
  - Word-boundary raw-text presence is robust against the mirrors' ad-hoc
    structure (commented nested YAML in the example; Markdown tables + YAML
    blocks + prose in the docs). A structure parser produced false-negatives
    in development; raw presence does not.

Known limitation (acceptable for a drift guard): a *new* field that reuses an
*existing* field name (e.g. another `timeout`) is not caught by the name check
alone — but a new top-level section or a genuinely new field name is. The
top-level section check below backstops the most impactful drift.
"""
from __future__ import annotations

import re
from pathlib import Path

from reyn.config.config_schema import walk_config_schema

_REPO_ROOT = Path(__file__).resolve().parents[1]
_EXAMPLE = _REPO_ROOT / "reyn.local.yaml.example"
_DOCS = _REPO_ROOT / "docs" / "reference" / "config" / "reyn-yaml.md"


def _present(name: str, text: str) -> bool:
    """True when *name* appears as a whole word in *text*."""
    return re.search(r"\b" + re.escape(name) + r"\b", text) is not None


def _schema_field_names() -> set[str]:
    """Every distinct leaf field-name in the ReynConfig schema."""
    return {n.key.rsplit(".", 1)[-1] for n in walk_config_schema()}


def _schema_top_level() -> set[str]:
    """Every top-level ReynConfig section/field name."""
    return {n.key.split(".", 1)[0] for n in walk_config_schema()}


def test_example_documents_every_config_field() -> None:
    """Tier 2: reyn.local.yaml.example mentions every config field name + section.

    A field added to ReynConfig but never added to the example template fails
    here — the example is advertised as an "exhaustive" mirror, so a silent gap
    is drift. Derives the field set from the live schema (no hand-enumeration).
    """
    text = _EXAMPLE.read_text(encoding="utf-8")
    missing_fields = sorted(n for n in _schema_field_names() if not _present(n, text))
    missing_top = sorted(t for t in _schema_top_level() if not _present(t, text))
    assert not missing_fields and not missing_top, (
        f"reyn.local.yaml.example is missing config fields {missing_fields} "
        f"and top-level sections {missing_top} — add a documented block "
        f"mirroring the new ReynConfig field(s)."
    )


def test_docs_documents_every_config_field() -> None:
    """Tier 2: docs/reference/config/reyn-yaml.md mentions every field name + section.

    Same drift guard for the reference doc. A new config field must appear in
    the reference (a table row, YAML example, or prose mention) — derived from
    the live schema, zero hand-enumeration.
    """
    text = _DOCS.read_text(encoding="utf-8")
    missing_fields = sorted(n for n in _schema_field_names() if not _present(n, text))
    missing_top = sorted(t for t in _schema_top_level() if not _present(t, text))
    assert not missing_fields and not missing_top, (
        f"docs/reference/config/reyn-yaml.md is missing config fields "
        f"{missing_fields} and top-level sections {missing_top} — document the "
        f"new ReynConfig field(s) in the reference."
    )


def test_mirror_files_exist() -> None:
    """Tier 2: the two mirror files exist at their expected paths.

    Guards the path constants above against a future move that would make the
    coverage checks silently vacuous (file unreadable → test error, not pass).
    """
    assert _EXAMPLE.is_file(), f"missing config example mirror: {_EXAMPLE}"
    assert _DOCS.is_file(), f"missing config reference doc: {_DOCS}"
