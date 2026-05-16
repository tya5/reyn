"""Tier 1 contract tests for FP-0036 Component D: feature-map coverage matrix.

Tests use real ScenarioSet instances constructed in-memory and the real
docs/feature-map.md. No MagicMock / AsyncMock / patch.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from reyn.dogfood.coverage import (
    CoverageMatrix,
    FeatureNode,
    compute_coverage,
    parse_feature_map,
)
from reyn.dogfood.scenarios import Scenario, ScenarioSet

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).parent.parent
FEATURE_MAP_PATH = REPO_ROOT / "docs" / "feature-map.md"


def _make_set(name: str, scenarios: list[Scenario]) -> ScenarioSet:
    """Build a ScenarioSet in-memory without touching the filesystem."""
    return ScenarioSet(name=name, scenarios=scenarios)


def _make_scenario(scenario_id: str, covers: list[str]) -> Scenario:
    """Build a minimal Scenario with the given covers tags."""
    return Scenario(id=scenario_id, covers=covers, input="test prompt")


# ---------------------------------------------------------------------------
# parse_feature_map
# ---------------------------------------------------------------------------


class TestParseFeatureMap:
    def test_returns_more_than_50_features(self) -> None:
        """Tier 1: parse_feature_map returns at least 50 features from the real doc."""
        features = parse_feature_map(FEATURE_MAP_PATH)
        assert len(features) > 50, (
            f"Expected >50 features, got {len(features)}. "
            "The real docs/feature-map.md has many entries."
        )

    def test_known_path_os_core_phase_engine_act_decide_loop(self) -> None:
        """Tier 1: 'os-core/phase-engine/act-decide-loop' is parsed from the table row."""
        features = parse_feature_map(FEATURE_MAP_PATH)
        paths = {f.path for f in features}
        assert "os-core/phase-engine/act-decide-loop" in paths, (
            f"Expected 'os-core/phase-engine/act-decide-loop' in feature paths. "
            f"Got paths sample: {sorted(paths)[:10]}"
        )

    def test_known_path_control_ir_ops_file(self) -> None:
        """Tier 1: 'control-ir-ops/file' is parsed from the Control IR Ops table."""
        features = parse_feature_map(FEATURE_MAP_PATH)
        paths = {f.path for f in features}
        assert "control-ir-ops/file" in paths, (
            f"Expected 'control-ir-ops/file' in feature paths. "
            f"Got paths sample: {sorted(p for p in paths if 'control' in p)[:10]}"
        )

    def test_known_path_stdlib_skills_eval(self) -> None:
        """Tier 1: 'stdlib-skills/eval' is parsed from the Stdlib Skills table."""
        features = parse_feature_map(FEATURE_MAP_PATH)
        paths = {f.path for f in features}
        assert "stdlib-skills/eval" in paths, (
            f"Expected 'stdlib-skills/eval' in feature paths. "
            f"Got stdlib paths: {sorted(p for p in paths if 'stdlib' in p)[:10]}"
        )

    def test_known_path_cli_reyn_run(self) -> None:
        """Tier 1: 'cli/reyn-run' is parsed from the CLI table."""
        features = parse_feature_map(FEATURE_MAP_PATH)
        paths = {f.path for f in features}
        assert "cli/reyn-run" in paths, (
            f"Expected 'cli/reyn-run' in feature paths. "
            f"Got cli paths: {sorted(p for p in paths if 'cli' in p)[:10]}"
        )

    def test_nodes_have_correct_parent_structure(self) -> None:
        """Tier 1: Section nodes have None parent; leaf nodes have non-None parent."""
        features = parse_feature_map(FEATURE_MAP_PATH)
        by_path = {f.path: f for f in features}

        # os-core is a top-level section: parent must be None
        assert "os-core" in by_path
        assert by_path["os-core"].parent is None

        # os-core/phase-engine is a subsection under os-core
        if "os-core/phase-engine" in by_path:
            assert by_path["os-core/phase-engine"].parent == "os-core"

        # os-core/phase-engine/act-decide-loop is a leaf under os-core/phase-engine
        leaf = by_path.get("os-core/phase-engine/act-decide-loop")
        if leaf is not None:
            assert leaf.parent == "os-core/phase-engine"

    def test_no_duplicate_paths(self) -> None:
        """Tier 1: parse_feature_map never returns duplicate paths."""
        features = parse_feature_map(FEATURE_MAP_PATH)
        paths = [f.path for f in features]
        assert len(paths) == len(set(paths)), (
            f"Duplicate feature paths found: "
            f"{[p for p in paths if paths.count(p) > 1][:5]}"
        )

    def test_no_empty_paths(self) -> None:
        """Tier 1: All returned nodes have non-empty path strings."""
        features = parse_feature_map(FEATURE_MAP_PATH)
        empty = [f for f in features if not f.path.strip()]
        assert not empty, f"Found {len(empty)} nodes with empty paths"

    def test_labels_preserved(self) -> None:
        """Tier 1: FeatureNode.label preserves the original text from the doc."""
        features = parse_feature_map(FEATURE_MAP_PATH)
        by_path = {f.path: f for f in features}
        node = by_path.get("os-core/phase-engine/act-decide-loop")
        if node is not None:
            assert "Act" in node.label or "act" in node.label.lower()


# ---------------------------------------------------------------------------
# compute_coverage — empty sets
# ---------------------------------------------------------------------------


class TestComputeCoverageEmpty:
    def test_empty_sets_all_features_uncovered(self) -> None:
        """Tier 1: compute_coverage with empty sets list → all features uncovered."""
        matrix = compute_coverage([], FEATURE_MAP_PATH)
        assert len(matrix.features) > 0
        assert matrix.covered_count == 0
        assert len(matrix.uncovered) == len(matrix.features)

    def test_empty_sets_no_unknown_tags(self) -> None:
        """Tier 1: compute_coverage with empty sets → unknown_tags is empty."""
        matrix = compute_coverage([], FEATURE_MAP_PATH)
        assert matrix.unknown_tags == []

    def test_empty_set_scenario_list(self) -> None:
        """Tier 1: A ScenarioSet with no scenarios contributes nothing."""
        empty_set = _make_set("empty", [])
        matrix = compute_coverage([empty_set], FEATURE_MAP_PATH)
        assert matrix.covered_count == 0


# ---------------------------------------------------------------------------
# compute_coverage — coverage assignment
# ---------------------------------------------------------------------------


class TestComputeCoverageAssignment:
    def test_matching_tag_marks_feature_covered(self) -> None:
        """Tier 1: A scenario covering 'os-core/phase-engine/act-decide-loop' marks it covered."""
        scenario = _make_scenario("s1", ["os-core/phase-engine/act-decide-loop"])
        s = _make_set("test_set", [scenario])
        matrix = compute_coverage([s], FEATURE_MAP_PATH)
        assert "os-core/phase-engine/act-decide-loop" in matrix.coverage_map
        refs = matrix.coverage_map["os-core/phase-engine/act-decide-loop"]
        assert len(refs) == 1
        assert refs[0] == ("test_set", "s1")

    def test_covered_count_accurate(self) -> None:
        """Tier 1: covered_count equals the number of paths with at least one scenario ref."""
        scenario = _make_scenario(
            "s1",
            [
                "os-core/phase-engine/act-decide-loop",
                "control-ir-ops/file",
            ],
        )
        s = _make_set("test_set", [scenario])
        matrix = compute_coverage([s], FEATURE_MAP_PATH)
        assert matrix.covered_count == 2

    def test_uncovered_excludes_covered_features(self) -> None:
        """Tier 1: uncovered list does not include features that have coverage refs."""
        target = "os-core/phase-engine/act-decide-loop"
        scenario = _make_scenario("s1", [target])
        s = _make_set("test_set", [scenario])
        matrix = compute_coverage([s], FEATURE_MAP_PATH)
        uncovered_paths = {f.path for f in matrix.uncovered}
        assert target not in uncovered_paths

    def test_multiple_sets_aggregate_coverage(self) -> None:
        """Tier 1: Coverage refs from multiple sets accumulate in coverage_map."""
        s1 = _make_set("set_a", [_make_scenario("sa1", ["control-ir-ops/file"])])
        s2 = _make_set("set_b", [_make_scenario("sb1", ["control-ir-ops/file"])])
        matrix = compute_coverage([s1, s2], FEATURE_MAP_PATH)
        refs = matrix.coverage_map["control-ir-ops/file"]
        assert len(refs) == 2
        set_names = {r[0] for r in refs}
        assert set_names == {"set_a", "set_b"}

    def test_scenario_with_no_covers_adds_nothing(self) -> None:
        """Tier 1: A scenario with empty covers list does not change coverage_map."""
        scenario = _make_scenario("s1", [])
        s = _make_set("test_set", [scenario])
        matrix = compute_coverage([s], FEATURE_MAP_PATH)
        assert matrix.covered_count == 0


# ---------------------------------------------------------------------------
# Unknown tags
# ---------------------------------------------------------------------------


class TestUnknownTags:
    def test_unknown_tag_recorded_in_matrix(self) -> None:
        """Tier 1: A covers tag with no matching feature path appears in unknown_tags."""
        bad_tag = "nonexistent/feature/path"
        scenario = _make_scenario("s1", [bad_tag])
        s = _make_set("test_set", [scenario])
        matrix = compute_coverage([s], FEATURE_MAP_PATH)
        found = [(sn, sid, tag) for sn, sid, tag in matrix.unknown_tags if tag == bad_tag]
        assert found, (
            f"Expected {bad_tag!r} in unknown_tags, got: {matrix.unknown_tags[:5]}"
        )

    def test_unknown_tag_attributes_correct(self) -> None:
        """Tier 1: unknown_tags tuple contains (set_name, scenario_id, tag)."""
        scenario = _make_scenario("s_bad", ["no/such/tag"])
        s = _make_set("my_set", [scenario])
        matrix = compute_coverage([s], FEATURE_MAP_PATH)
        assert ("my_set", "s_bad", "no/such/tag") in matrix.unknown_tags

    def test_valid_and_invalid_tags_mixed(self) -> None:
        """Tier 1: Valid tags are covered; invalid tags go to unknown_tags."""
        scenario = _make_scenario(
            "s_mixed",
            ["os-core/phase-engine/act-decide-loop", "totally/invalid/path"],
        )
        s = _make_set("mixed_set", [scenario])
        matrix = compute_coverage([s], FEATURE_MAP_PATH)

        # Valid tag is covered
        assert matrix.covered_count >= 1
        refs = matrix.coverage_map["os-core/phase-engine/act-decide-loop"]
        assert len(refs) == 1

        # Invalid tag is in unknown_tags
        assert ("mixed_set", "s_mixed", "totally/invalid/path") in matrix.unknown_tags


# ---------------------------------------------------------------------------
# to_json
# ---------------------------------------------------------------------------


class TestToJson:
    def test_to_json_is_json_serialisable(self) -> None:
        """Tier 1: CoverageMatrix.to_json() returns a JSON-serialisable dict."""
        scenario = _make_scenario("s1", ["os-core/phase-engine/act-decide-loop"])
        s = _make_set("test_set", [scenario])
        matrix = compute_coverage([s], FEATURE_MAP_PATH)
        result = matrix.to_json()
        # Must not raise
        serialised = json.dumps(result)
        assert isinstance(serialised, str)

    def test_to_json_structure(self) -> None:
        """Tier 1: to_json() has required top-level keys."""
        matrix = compute_coverage([], FEATURE_MAP_PATH)
        result = matrix.to_json()
        required_keys = {
            "total_features",
            "covered_count",
            "uncovered_count",
            "coverage_map",
            "uncovered",
            "unknown_tags",
        }
        assert required_keys.issubset(result.keys()), (
            f"Missing keys: {required_keys - set(result.keys())}"
        )

    def test_to_json_counts_consistent(self) -> None:
        """Tier 1: to_json() counts match the matrix properties."""
        scenario = _make_scenario("s1", ["control-ir-ops/file"])
        s = _make_set("test_set", [scenario])
        matrix = compute_coverage([s], FEATURE_MAP_PATH)
        result = matrix.to_json()

        assert result["covered_count"] == matrix.covered_count
        assert result["uncovered_count"] == len(matrix.uncovered)
        assert result["total_features"] == len(matrix.features)
        assert result["covered_count"] + result["uncovered_count"] == result["total_features"]

    def test_to_json_empty_sets_all_uncovered(self) -> None:
        """Tier 1: to_json() with empty sets shows all features uncovered."""
        matrix = compute_coverage([], FEATURE_MAP_PATH)
        result = matrix.to_json()
        assert result["covered_count"] == 0
        assert result["uncovered_count"] == result["total_features"]

    def test_to_json_unknown_tags_present(self) -> None:
        """Tier 1: to_json() unknown_tags list contains the bad tag entry."""
        scenario = _make_scenario("s1", ["ghost/path"])
        s = _make_set("ghost_set", [scenario])
        matrix = compute_coverage([s], FEATURE_MAP_PATH)
        result = matrix.to_json()
        tags = [entry["tag"] for entry in result["unknown_tags"]]
        assert "ghost/path" in tags
