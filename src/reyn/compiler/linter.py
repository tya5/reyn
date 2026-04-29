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


def lint_skill(path: Path, known_artifacts: set[str]) -> list[LintIssue]:
    issues: list[LintIssue] = []
    from .parser import parse_skill
    try:
        app_def = parse_skill(path)
    except KeyError as exc:
        return [LintIssue("error", path, f"Missing required field {exc}")]
    except Exception as exc:
        return [LintIssue("error", path, f"Parse error: {exc}")]

    app_dir = path.parent
    phase_files = {p.stem for p in (app_dir / "phases").glob("*.md")} if (app_dir / "phases").exists() else set()

    # name
    if not app_def.name:
        issues.append(LintIssue("error", path, "Missing required field 'name'"))
    elif not _is_snake(app_def.name):
        issues.append(LintIssue("warning", path, f"Name '{app_def.name}' should be snake_case"))

    # entry
    if not app_def.entry:
        issues.append(LintIssue("error", path, "Missing required field 'entry'"))
    elif app_def.entry not in phase_files:
        issues.append(LintIssue("error", path, f"entry '{app_def.entry}' not found in phases/"))

    # final_output
    if not app_def.final_output:
        issues.append(LintIssue("error", path, "Missing required field 'final_output'"))
    elif app_def.final_output not in known_artifacts:
        issues.append(LintIssue(
            "error", path,
            f"final_output '{app_def.final_output}' not found in known artifacts.",
        ))

    # graph: all referenced phases must exist
    for src, dst in app_def.edges:
        for node in (src, dst):
            if node.startswith("@"):
                continue
            if node not in phase_files:
                issues.append(LintIssue(
                    "error", path,
                    f"Graph references phase '{node}' but no phases/{node}.md found.",
                ))

    # graph: entry must appear in the graph (skip for single-phase apps with no edges)
    if app_def.edges:
        all_graph_nodes = {n for edge in app_def.edges for n in edge if not n.startswith("@")}
        if app_def.entry and app_def.entry not in all_graph_nodes:
            issues.append(LintIssue(
                "warning", path,
                f"entry '{app_def.entry}' does not appear in graph — is it connected?",
            ))

    # graph: DAG check
    cycle = _find_cycle(app_def.edges)
    if cycle is not None:
        cycle_str = " → ".join(cycle)
        issues.append(LintIssue(
            "error", path,
            f"Graph contains a cycle: {cycle_str}. "
            "Use control.type='rollback' for revision loops instead of back-edges.",
        ))

    return issues


# ── Plan-level lint (operates on app_structure / app_plan dicts) ─────────────

def lint_plan(plan: dict) -> list[str]:
    """
    Run deterministic structural checks on an app_structure / app_plan dict.
    Returns a list of human-readable issue strings (empty if clean).

    Checks:
      - graph cycles (transitions form a DAG)
      - input_artifact coverage (every phase's input_artifact must be declared
        in `artifacts`, or be the stdlib `user_message`, or the final_output)
      - entry_phase exists in phases
      - transition endpoints reference declared phases
    """
    issues: list[str] = []

    transitions = plan.get("transitions") or []
    edges: list[tuple[str, str]] = []
    for t in transitions:
        if not isinstance(t, dict):
            continue
        src = t.get("from")
        for dst in t.get("to") or []:
            if src and dst:
                edges.append((src, dst))

    cycle = _find_cycle(edges)
    if cycle is not None:
        cycle_str = " → ".join(cycle)
        issues.append(
            f"Graph contains a cycle: {cycle_str}. "
            "Use control.type='rollback' for revision loops instead of back-edges."
        )

    phases = plan.get("phases") or []
    phase_names = {p.get("name") for p in phases if isinstance(p, dict) and p.get("name")}

    artifact_names: set[str] = set()
    for a in plan.get("artifacts") or []:
        if isinstance(a, dict) and a.get("name"):
            artifact_names.add(a["name"])
    final_output = plan.get("final_output") or {}
    if isinstance(final_output, dict) and final_output.get("name"):
        artifact_names.add(final_output["name"])
    artifact_names.add("user_message")  # stdlib

    for p in phases:
        if not isinstance(p, dict):
            continue
        input_artifact = p.get("input_artifact")
        if input_artifact and input_artifact not in artifact_names:
            issues.append(
                f"Phase '{p.get('name')}' references input_artifact "
                f"'{input_artifact}' but it is not declared in `artifacts` "
                f"(and is not the stdlib `user_message` or `final_output`)."
            )

    entry_phase = plan.get("entry_phase")
    if entry_phase and entry_phase not in phase_names:
        issues.append(
            f"entry_phase '{entry_phase}' is not declared in `phases`."
        )

    for t in transitions:
        if not isinstance(t, dict):
            continue
        src = t.get("from")
        if src and src not in phase_names:
            issues.append(f"Transition source '{src}' is not declared in `phases`.")
        for dst in t.get("to") or []:
            if dst and dst not in phase_names:
                issues.append(f"Transition target '{dst}' is not declared in `phases`.")

    return issues


# ── DSL root ──────────────────────────────────────────────────────────────────

def lint_skill_dir(skill_dir: Path) -> list[LintIssue]:
    """Lint the skill at skill_dir (must contain skill.md).

    Stdlib artifacts/phases are loaded as known names for reference resolution
    but are NOT themselves linted — only the target skill's own files are checked.
    """
    app_dir = skill_dir  # internal alias
    issues: list[LintIssue] = []

    from .loader import _stdlib_dir

    # ── known names (for reference resolution only, not linted) ──────────────
    reference_dirs: list[Path] = [
        _stdlib_dir("artifacts"),
        app_dir.parent.parent / "shared" / "artifacts",  # reyn/shared/artifacts if exists
    ]
    artifact_names: set[str] = set()
    for d in reference_dirs:
        if d.exists():
            for p in sorted(d.glob("*.yaml")):
                try:
                    art = parse_artifact(p)
                    artifact_names.add(art.name)
                except Exception:
                    pass  # broken stdlib files are not this app's problem

    # ── app-owned artifacts (collected first so names are known before lint) ──
    app_artifact_dir = app_dir / "artifacts"
    app_artifact_paths: list[Path] = []
    if app_artifact_dir.exists():
        for p in sorted(app_artifact_dir.glob("*.yaml")):
            try:
                art = parse_artifact(p)
                artifact_names.add(art.name)
                app_artifact_paths.append(p)
            except Exception as exc:
                issues.append(LintIssue("error", p, f"Parse error: {exc}"))

    # ── lint app-owned artifacts ──────────────────────────────────────────────
    for p in app_artifact_paths:
        try:
            issues.extend(lint_artifact(p, artifact_names))
        except Exception as exc:
            issues.append(LintIssue("error", p, f"Lint error: {exc}"))

    # ── lint app-owned phases ─────────────────────────────────────────────────
    app_phase_dir = app_dir / "phases"
    if app_phase_dir.exists():
        for p in sorted(app_phase_dir.glob("*.md")):
            try:
                issues.extend(lint_phase(p, artifact_names))
            except Exception as exc:
                issues.append(LintIssue("error", p, f"Lint error: {exc}"))

    # ── lint app graph ────────────────────────────────────────────────────────
    app_md = app_dir / "skill.md"
    if app_md.exists():
        try:
            issues.extend(lint_skill(app_md, artifact_names))
        except Exception as exc:
            issues.append(LintIssue("error", app_md, f"Lint error: {exc}"))

    return issues
