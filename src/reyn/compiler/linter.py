"""
DSL Linter

Checks DSL files for consistency issues that would make Meta-App generation unreliable.
Does not compile; reports issues without crashing.
"""
from __future__ import annotations
import re
from dataclasses import dataclass
from pathlib import Path

import jsonschema

from .parser import _split_frontmatter, parse_artifact

PHASE_FRONTMATTER_ORDER = ["type", "name", "input", "role", "can_finish"]
_SNAKE_CASE = re.compile(r"^[a-z][a-z0-9_]*$")


@dataclass
class LintIssue:
    severity: str  # "error" | "warning"
    path: Path
    message: str

    def __str__(self) -> str:
        rel = self.path
        return f"[{self.severity.upper():7}] {rel}  →  {self.message}"


def _is_snake(name: str) -> bool:
    return bool(_SNAKE_CASE.match(name))


# ── Artifact ──────────────────────────────────────────────────────────────────

def lint_artifact(path: Path, known_artifact_names: set[str]) -> list[LintIssue]:
    issues: list[LintIssue] = []
    import yaml
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception as exc:
        issues.append(LintIssue("error", path, f"Not valid YAML: {exc}"))
        return issues

    if not isinstance(data, dict):
        issues.append(LintIssue("error", path, "Artifact file must be a YAML object"))
        return issues

    name = data.get("name", "")
    if not name:
        issues.append(LintIssue("error", path, "Missing 'name' key"))
    elif not _is_snake(name):
        issues.append(LintIssue("warning", path, f"Name '{name}' should be snake_case"))

    schema = data.get("schema")
    if not schema:
        issues.append(LintIssue("error", path, "Missing 'schema' key — must be a JSON Schema object"))
        return issues

    if not isinstance(schema, dict):
        issues.append(LintIssue("error", path, "'schema' must be a YAML/JSON object"))
        return issues

    try:
        jsonschema.Draft7Validator.check_schema(schema)
    except jsonschema.SchemaError as exc:
        issues.append(LintIssue("error", path, f"Invalid JSON Schema: {exc.message}"))
        return issues

    if schema.get("type") != "object":
        issues.append(LintIssue("warning", path, "Artifact schema should have 'type: object' at top level"))

    return issues


# ── Phase ─────────────────────────────────────────────────────────────────────

def lint_phase(path: Path, known_artifacts: set[str]) -> list[LintIssue]:
    issues: list[LintIssue] = []
    text = path.read_text(encoding="utf-8")
    fm, body = _split_frontmatter(text)

    for key in ("type", "name"):
        if key not in fm:
            issues.append(LintIssue("error", path, f"Missing required frontmatter key '{key}'"))

    name = fm.get("name", "")
    if name and not _is_snake(name):
        issues.append(LintIssue("warning", path, f"Name '{name}' should be snake_case"))

    # Frontmatter key ordering
    actual = [k for k in fm if k in PHASE_FRONTMATTER_ORDER]
    expected = [k for k in PHASE_FRONTMATTER_ORDER if k in fm]
    if actual != expected:
        issues.append(LintIssue(
            "warning", path,
            f"Frontmatter keys not in canonical order. "
            f"Expected {expected}, got {actual}",
        ))

    if "output" in fm:
        issues.append(LintIssue(
            "error", path,
            f"Phase '{fm.get('name', path.stem)}' must not define output. "
            "Output schema is provided at runtime from candidate next phase input schemas "
            "or app final output schema.",
        ))

    # Input artifact resolution
    inputs_raw = str(fm.get("input") or "")
    inputs = [i.strip() for i in inputs_raw.split("|") if i.strip()]
    for inp in inputs:
        if inp not in known_artifacts:
            issues.append(LintIssue("error", path, f"Input artifact '{inp}' not found"))

    if not body.strip():
        issues.append(LintIssue("warning", path, "Phase has no instructions"))

    return issues


# ── App ───────────────────────────────────────────────────────────────────────

def _find_cycle(edges: list[tuple[str, str]]) -> list[str] | None:
    """Return the cycle path as a list of node names, or None if the graph is acyclic."""
    adjacency: dict[str, list[str]] = {}
    for src, dst in edges:
        adjacency.setdefault(src, []).append(dst)
        adjacency.setdefault(dst, [])  # ensure every node appears

    visited: set[str] = set()
    path: list[str] = []
    on_path: set[str] = set()

    def dfs(node: str) -> list[str] | None:
        visited.add(node)
        path.append(node)
        on_path.add(node)
        for neighbour in adjacency.get(node, []):
            if neighbour not in visited:
                result = dfs(neighbour)
                if result is not None:
                    return result
            elif neighbour in on_path:
                # Found cycle — extract the cycle portion from path
                cycle_start = path.index(neighbour)
                return path[cycle_start:] + [neighbour]
        path.pop()
        on_path.discard(node)
        return None

    for node in list(adjacency):
        if node not in visited:
            result = dfs(node)
            if result is not None:
                return result
    return None


def lint_app(path: Path, known_artifacts: set[str]) -> list[LintIssue]:
    issues: list[LintIssue] = []
    from .parser import parse_app
    try:
        app_def = parse_app(path)
    except Exception as exc:
        return [LintIssue("error", path, f"Parse error: {exc}")]

    app_dir = path.parent
    phase_files = {p.stem for p in (app_dir / "phases").glob("*.md")} if (app_dir / "phases").exists() else set()

    for src, dst in app_def.edges:
        for node in (src, dst):
            if node.startswith("@"):
                continue
            if node not in phase_files:
                issues.append(LintIssue(
                    "error", path,
                    f"Graph references phase '{node}' but no phases/{node}.md found. "
                    "Use can_finish: true on the delivering phase instead of a 'finish' node.",
                ))

    if app_def.final_output and app_def.final_output not in known_artifacts:
        issues.append(LintIssue(
            "error", path,
            f"final_output '{app_def.final_output}' not found in known artifacts.",
        ))

    # Graph must be a DAG — cycles are expressed via OS rollback, not graph edges
    cycle = _find_cycle(app_def.edges)
    if cycle is not None:
        cycle_str = " → ".join(cycle)
        issues.append(LintIssue(
            "error", path,
            f"Graph contains a cycle: {cycle_str}. "
            "Use control.type='rollback' for revision loops instead of back-edges.",
        ))

    return issues


# ── DSL root ──────────────────────────────────────────────────────────────────

def lint_dsl(dsl_root: Path) -> list[LintIssue]:
    """Lint all artifacts and phases under dsl_root."""
    issues: list[LintIssue] = []

    from .loader import _stdlib_dir
    artifact_dirs: list[Path] = [_stdlib_dir("artifacts"), dsl_root / "shared" / "artifacts"]
    apps_root = dsl_root / "apps"
    if apps_root.exists():
        artifact_dirs += sorted(apps_root.glob("*/artifacts"))

    phase_dirs: list[Path] = [_stdlib_dir("phases"), dsl_root / "shared" / "phases"]
    if apps_root.exists():
        phase_dirs += sorted(apps_root.glob("*/phases"))

    # Build known artifact names
    artifact_names: set[str] = set()
    artifact_paths: list[Path] = []
    for d in artifact_dirs:
        if d.exists():
            for p in sorted(d.glob("*.yaml")):
                try:
                    art = parse_artifact(p)
                    artifact_names.add(art.name)
                    artifact_paths.append(p)
                except Exception as exc:
                    issues.append(LintIssue("error", p, f"Parse error: {exc}"))

    # Lint artifacts
    for p in artifact_paths:
        try:
            issues.extend(lint_artifact(p, artifact_names))
        except Exception as exc:
            issues.append(LintIssue("error", p, f"Lint error: {exc}"))

    # Lint phases
    for d in phase_dirs:
        if not d.exists():
            continue
        for p in sorted(d.glob("*.md")):
            try:
                issues.extend(lint_phase(p, artifact_names))
            except Exception as exc:
                issues.append(LintIssue("error", p, f"Lint error: {exc}"))

    # Lint app graphs
    if apps_root.exists():
        for app_md in sorted(apps_root.glob("*/app.md")):
            try:
                issues.extend(lint_app(app_md, artifact_names))
            except Exception as exc:
                issues.append(LintIssue("error", app_md, f"Lint error: {exc}"))

    return issues
