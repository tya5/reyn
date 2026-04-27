"""
DSL Linter

Checks DSL files for consistency issues that would make Meta-App generation unreliable.
Does not compile; reports issues without crashing.
"""
from __future__ import annotations
import re
from dataclasses import dataclass
from pathlib import Path

from .parser import _split_frontmatter, _parse_fields, parse_artifact
from .expander import _TYPE_MAP

PHASE_FRONTMATTER_ORDER = ["type", "name", "input", "input_description", "role", "can_finish"]
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

def lint_artifact(path: Path, known_types: set[str]) -> list[LintIssue]:
    issues: list[LintIssue] = []
    text = path.read_text(encoding="utf-8")
    fm, body = _split_frontmatter(text)

    name = fm.get("name", "")
    if not name:
        issues.append(LintIssue("error", path, "Missing 'name' in frontmatter"))
    elif not _is_snake(name):
        issues.append(LintIssue("warning", path, f"Name '{name}' should be snake_case"))

    fields = _parse_fields(body)
    passed_optional = False
    for f in fields:
        if not f.optional:
            if passed_optional:
                issues.append(LintIssue(
                    "warning", path,
                    f"Required field '{f.name}' appears after an optional field — "
                    "required fields should come first",
                ))
        else:
            passed_optional = True

        if not _is_snake(f.name):
            issues.append(LintIssue("warning", path, f"Field name '{f.name}' should be snake_case"))

        if f.schema is None and f.type_str not in known_types:
            issues.append(LintIssue(
                "error", path,
                f"Field '{f.name}' has unknown type '{f.type_str}'"
            ))

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
        if inp not in known_artifacts and inp not in _TYPE_MAP:
            issues.append(LintIssue("error", path, f"Input artifact '{inp}' not found"))

    if not body.strip():
        issues.append(LintIssue("warning", path, "Phase has no instructions"))

    return issues


# ── App ───────────────────────────────────────────────────────────────────────

def lint_app(path: Path, known_artifacts: set[str]) -> list[LintIssue]:
    issues: list[LintIssue] = []
    from .parser import parse_app
    try:
        app_def = parse_app(path)
    except Exception as exc:
        return [LintIssue("error", path, f"Parse error: {exc}")]

    app_dir = path.parent
    phase_files = {p.stem for p in (app_dir / "phases").glob("*.md")} if (app_dir / "phases").exists() else set()

    # Check each graph node that is a regular phase (not an @app_node)
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

    # Check final_output artifact exists
    if app_def.final_output and app_def.final_output not in known_artifacts:
        issues.append(LintIssue(
            "error", path,
            f"final_output '{app_def.final_output}' not found in known artifacts.",
        ))

    return issues


# ── DSL root ──────────────────────────────────────────────────────────────────

def lint_dsl(dsl_root: Path) -> list[LintIssue]:
    """Lint all artifacts and phases under dsl_root."""
    issues: list[LintIssue] = []

    # Collect artifact search directories: stdlib first, then shared, then per-app
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
            for p in sorted(d.glob("*.md")):
                try:
                    art = parse_artifact(p)
                    artifact_names.add(art.name)
                    artifact_paths.append(p)
                except Exception as exc:
                    issues.append(LintIssue("error", p, f"Parse error: {exc}"))

    known_types = set(_TYPE_MAP.keys()) | artifact_names

    # Lint artifacts
    for p in artifact_paths:
        try:
            issues.extend(lint_artifact(p, known_types))
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
