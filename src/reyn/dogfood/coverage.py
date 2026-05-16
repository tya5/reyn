"""Feature-map coverage matrix for dogfood scenarios (FP-0036 Component D).

Parses ``docs/feature-map.md`` into a tree of feature paths, then walks one
or more ScenarioSets to determine which features are covered by which
scenarios. Surfaces uncovered features so authors can prioritise scenario
authoring.

Feature path scheme:
  - Top-level sections become roots (= os-core, control-ir-ops, dsl, ...)
  - Subsections become children (= os-core/phase-engine)
  - Table rows become leaves (= os-core/phase-engine/act-decide-loop)

Feature paths use lowercase kebab-case; punctuation stripped.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from reyn.dogfood.scenarios import ScenarioSet


def _to_kebab(text: str) -> str:
    """Convert a label to lowercase-kebab-case path component.

    Strips leading/trailing whitespace, lowercases, replaces runs of
    non-alphanumeric characters with a single hyphen, and trims leading/
    trailing hyphens.

    Examples:
      "OS Core"            -> "os-core"
      "Phase Engine"       -> "phase-engine"
      "Act/Decide loop"    -> "act-decide-loop"
      "113+ event types"   -> "113-event-types"
      "`run_op` step"      -> "run-op-step"
      "Tier 0 — always allowed" -> "tier-0-always-allowed"
    """
    text = text.strip()
    # Remove backtick markers used in Markdown inline code
    text = text.replace("`", "")
    # Replace any non-alphanumeric character (including _, +, /, —, spaces) with hyphen
    text = re.sub(r"[^a-zA-Z0-9]+", "-", text)
    text = text.strip("-").lower()
    return text


@dataclass
class FeatureNode:
    """One feature in the feature-map tree.

    path: lowercase-kebab full path (= "os-core/phase-engine/act-decide-loop")
    label: original human-readable label from the doc
    parent: parent path or None for roots
    """

    path: str
    label: str
    parent: str | None = None


@dataclass
class CoverageMatrix:
    """Coverage of a feature-map by one or more scenario sets."""

    features: list[FeatureNode] = field(default_factory=list)
    # path -> list of (set_name, scenario_id) that declare this in their covers
    coverage_map: dict[str, list[tuple[str, str]]] = field(default_factory=dict)
    # tags that appeared in scenarios but matched no feature path
    unknown_tags: list[tuple[str, str, str]] = field(default_factory=list)  # (set_name, scenario_id, tag)

    @property
    def covered_count(self) -> int:
        return sum(1 for refs in self.coverage_map.values() if refs)

    @property
    def uncovered(self) -> list[FeatureNode]:
        return [f for f in self.features if not self.coverage_map.get(f.path)]

    def to_json(self) -> dict[str, Any]:
        """Return a JSON-serialisable dict summarising coverage."""
        return {
            "total_features": len(self.features),
            "covered_count": self.covered_count,
            "uncovered_count": len(self.uncovered),
            "coverage_map": {
                path: [{"set_name": s, "scenario_id": sid} for s, sid in refs]
                for path, refs in self.coverage_map.items()
            },
            "uncovered": [
                {"path": f.path, "label": f.label, "parent": f.parent}
                for f in self.uncovered
            ],
            "unknown_tags": [
                {"set_name": s, "scenario_id": sid, "tag": tag}
                for s, sid, tag in self.unknown_tags
            ],
        }


# ---------------------------------------------------------------------------
# Markdown parser
# ---------------------------------------------------------------------------

_H3_RE = re.compile(r"^###\s+(.+)$")
_H4_RE = re.compile(r"^####\s+(.+)$")
_TABLE_ROW_RE = re.compile(r"^\|(.+)\|$")
_TABLE_SEP_RE = re.compile(r"^\|[-| :]+\|$")  # separator like |---|---|---|
_MERMAID_OPEN = re.compile(r"^\s*```mermaid\s*$")
_CODE_CLOSE = re.compile(r"^\s*```\s*$")


def parse_feature_map(path: str | Path) -> list[FeatureNode]:
    """Parse docs/feature-map.md into a flat list of FeatureNode.

    Strategy:
      - Skip the mindmap block (= between ```mermaid and ``` markers)
      - Walk Markdown headings (### / ####) for sections
      - Pull table rows (| ... | ... | ... |) for leaf features
      - Map heading text to kebab-case for the path component

    The resulting list is ordered depth-first (section nodes appear before
    their leaf children).  Intermediate section nodes ARE included so that
    callers can detect partial coverage at the section level if desired.
    """
    path = Path(path)
    lines = path.read_text(encoding="utf-8").splitlines()

    nodes: list[FeatureNode] = []
    # Track headings at each level so we can build paths
    current_h3: str | None = None  # kebab path of current ### section
    current_h3_label: str | None = None
    current_h4: str | None = None  # kebab path of current #### subsection
    current_h4_label: str | None = None

    in_mermaid = False
    in_code = False  # generic code block (non-mermaid)
    seen_paths: set[str] = set()

    def _add(node: FeatureNode) -> None:
        if node.path not in seen_paths:
            nodes.append(node)
            seen_paths.add(node.path)

    for line in lines:
        # ── fence tracking ────────────────────────────────────────────────
        if _MERMAID_OPEN.match(line):
            in_mermaid = True
            continue
        if in_mermaid:
            if _CODE_CLOSE.match(line):
                in_mermaid = False
            continue

        # Skip generic code blocks (non-mermaid) to avoid parsing code samples
        if not in_mermaid and line.strip().startswith("```") and not in_code:
            in_code = True
            continue
        if in_code:
            if _CODE_CLOSE.match(line):
                in_code = False
            continue

        # ── headings ──────────────────────────────────────────────────────
        m3 = _H3_RE.match(line)
        if m3:
            label = m3.group(1).strip()
            slug = _to_kebab(label)
            current_h3 = slug
            current_h3_label = label
            current_h4 = None
            current_h4_label = None
            _add(FeatureNode(path=slug, label=label, parent=None))
            continue

        m4 = _H4_RE.match(line)
        if m4:
            label = m4.group(1).strip()
            slug = _to_kebab(label)
            if current_h3 is not None:
                full_path = f"{current_h3}/{slug}"
                parent = current_h3
            else:
                full_path = slug
                parent = None
            current_h4 = full_path
            current_h4_label = label
            _add(FeatureNode(path=full_path, label=label, parent=parent))
            continue

        # ── table rows ────────────────────────────────────────────────────
        m_row = _TABLE_ROW_RE.match(line)
        if m_row:
            # Skip separator rows (|---|---|...)
            if _TABLE_SEP_RE.match(line):
                continue
            # Extract the first cell as the feature label
            cells = [c.strip() for c in line.split("|") if c.strip()]
            if not cells:
                continue
            label = cells[0]
            # Skip the header row (first cell is a heading word like "Feature", "Op", "Command" etc.)
            _header_words = {
                "feature", "op", "command", "skill", "block", "description",
                "documentation", "layer", "backend",
            }
            if label.lower() in _header_words:
                continue

            slug = _to_kebab(label)
            if not slug:
                continue

            # Determine parent
            if current_h4 is not None:
                full_path = f"{current_h4}/{slug}"
                parent = current_h4
            elif current_h3 is not None:
                full_path = f"{current_h3}/{slug}"
                parent = current_h3
            else:
                full_path = slug
                parent = None

            _add(FeatureNode(path=full_path, label=label, parent=parent))

    return nodes


# ---------------------------------------------------------------------------
# Coverage computation
# ---------------------------------------------------------------------------


def compute_coverage(
    sets: "list[ScenarioSet]",
    feature_map_path: str | Path,
) -> CoverageMatrix:
    """Build the coverage matrix.

    For each scenario across all sets, take its ``covers`` tags and assign
    the scenario to each matching feature path. Tags that don't match any
    feature path are recorded in CoverageMatrix.unknown_tags.
    """
    features = parse_feature_map(feature_map_path)
    known_paths: set[str] = {f.path for f in features}

    # Initialise coverage map with empty lists for every known feature
    coverage_map: dict[str, list[tuple[str, str]]] = {f.path: [] for f in features}
    unknown_tags: list[tuple[str, str, str]] = []

    for scenario_set in sets:
        set_name = scenario_set.name
        for scenario in scenario_set.scenarios:
            for tag in scenario.covers:
                if tag in known_paths:
                    coverage_map[tag].append((set_name, scenario.id))
                else:
                    unknown_tags.append((set_name, scenario.id, tag))

    return CoverageMatrix(
        features=features,
        coverage_map=coverage_map,
        unknown_tags=unknown_tags,
    )
