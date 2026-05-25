"""Tier 2: FP-0043 Phase 1 — embedding bench manifest + runner contract.

Pins the bench infrastructure shape so future edits to the manifest or
the runner don't silently break the measurement contract:

  1. Manifest loads as YAML with schema_version 1 + last_synced_commit_sha
     + a non-empty fixtures list.
  2. Each fixture carries id / source / prompt / expected_action /
     expected_reach_path / axis fields with the right types.
  3. The set of qualified_names referenced by precision-axis fixtures is
     a subset of the static-only catalog (= what a fresh-context router
     state without dynamic skills / exec backends surfaces). This pins
     the "fresh-user" framing of the bench — fixtures that would require
     post-discovery dynamic enumeration are explicitly out of scope here,
     and a future bench section can add them with router-state wiring.
  4. Fixture id is unique across the manifest.
  5. ``axis`` is restricted to the enum {"precision", "call_rate"}.

No mocks. Real manifest load + real LIST_ACTIONS handler enumeration.
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import yaml

from reyn.tools.types import ToolContext
from reyn.tools.universal_catalog import CATEGORIES, LIST_ACTIONS

_MANIFEST_PATH = (
    Path(__file__).parent.parent
    / "tests" / "data" / "embedding_bench" / "manifest.yaml"
)
_ALLOWED_AXES = {"precision", "call_rate"}


def _load_manifest() -> dict[str, Any]:
    return yaml.safe_load(_MANIFEST_PATH.read_text(encoding="utf-8"))


class _Events:
    def emit(self, *a: Any, **kw: Any) -> None:
        pass


async def _static_catalog() -> set[str]:
    ctx = ToolContext(
        events=_Events(),
        permission_resolver=None,
        workspace=None,
        caller_kind="router",
        router_state=None,
    )
    out: set[str] = set()
    for cat in CATEGORIES:
        page = await LIST_ACTIONS.handler({"category": [cat]}, ctx)
        for item in page.get("items", []):
            out.add(item["qualified_name"])
    return out


# ── 1. Manifest structural shape ──────────────────────────────────────────────


def test_manifest_loads_and_carries_schema_version_and_sha() -> None:
    """Tier 2: manifest.yaml parses and declares schema + drift-detection sha."""
    data = _load_manifest()
    assert data["schema_version"] == 1
    assert isinstance(data["last_synced_commit_sha"], str)
    assert len(data["last_synced_commit_sha"]) >= 40  # full git SHA
    assert isinstance(data["fixtures"], list)
    assert len(data["fixtures"]) >= 10  # the bench must not shrink silently


# ── 2. Per-fixture field contract ─────────────────────────────────────────────


def test_each_fixture_carries_required_fields() -> None:
    """Tier 2: every fixture entry has id / source / prompt / expected_action /
    expected_reach_path / axis with the right primitive types.
    """
    data = _load_manifest()
    for fx in data["fixtures"]:
        assert isinstance(fx["id"], str) and fx["id"]
        assert isinstance(fx["source"], str)
        assert isinstance(fx["prompt"], str) and fx["prompt"].strip()
        assert isinstance(fx["expected_action"], str) and "__" in fx["expected_action"]
        assert isinstance(fx["expected_reach_path"], list)
        assert all(isinstance(p, str) for p in fx["expected_reach_path"])
        assert isinstance(fx["axis"], list)
        assert set(fx["axis"]).issubset(_ALLOWED_AXES), (
            f"fixture {fx['id']!r}: axis={fx['axis']} contains values outside "
            f"{sorted(_ALLOWED_AXES)}"
        )


# ── 3. precision-axis fixtures all resolve in the static catalog ──────────────


def test_precision_axis_fixtures_resolve_in_static_catalog() -> None:
    """Tier 2: every precision-axis fixture's expected_action exists in the
    fresh-context (= router_state=None) catalog enumeration.

    This pins the "fresh-user precision" framing: bench measurement is over
    the catalog a brand-new ChatSession sees before any dynamic skill /
    sandbox-backed exec is wired. Fixtures that would require dynamic
    enumeration go into a future section with explicit router-state setup.
    """
    data = _load_manifest()
    precision_fixtures = [fx for fx in data["fixtures"] if "precision" in fx["axis"]]
    catalog = asyncio.run(_static_catalog())
    unknown = [
        (fx["id"], fx["expected_action"])
        for fx in precision_fixtures
        if fx["expected_action"] not in catalog
    ]
    assert not unknown, (
        f"{len(unknown)} precision-axis fixture(s) reference qualified names "
        f"absent from the static catalog (= dynamic-enumeration only). "
        f"Move them to a non-precision axis or wire up router-state for "
        f"dynamic enumeration in the bench runner:\n"
        + "\n".join(f"  - {fid}: {qn}" for fid, qn in unknown)
    )


# ── 4. Uniqueness ─────────────────────────────────────────────────────────────


def test_fixture_ids_are_unique() -> None:
    """Tier 2: fixture id collisions silently overwrite measurements; reject."""
    data = _load_manifest()
    ids = [fx["id"] for fx in data["fixtures"]]
    assert len(ids) == len(set(ids)), (
        f"duplicate fixture ids: "
        f"{[i for i in set(ids) if ids.count(i) > 1]}"
    )
