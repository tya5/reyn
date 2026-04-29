"""
Parser for eval.md spec files.

Format:
  ---
  type: eval
  app: dsl/apps/foo/app.md
  dsl_root: dsl/
  model: standard
  ---

  ## case: my_case
  input: "the test input text"

  ### phase: analyze_code
  quality:
  - criterion one
  - [aspirational] criterion two

  ### final
  quality:
  - criterion three
"""
from __future__ import annotations
import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml


@dataclass
class QualityCriterion:
    text: str
    tag: str = "required"  # "required" | "aspirational"


@dataclass
class PhaseCriteria:
    phase: str | None          # None = "final"
    schema: dict | None        # kept for backward compat but not used by new eval app
    criteria: list[QualityCriterion]


@dataclass
class CrossPhaseAssertion:
    phase_a: str
    path_a: str
    op: str
    phase_b: str
    path_b: str
    raw: str


@dataclass
class EvalCase:
    name: str
    input: str
    phase_criteria: list[PhaseCriteria]
    cross_phase: list[CrossPhaseAssertion] = field(default_factory=list)


@dataclass
class EvalSpec:
    app_dsl_path: str
    dsl_root: str | None
    model: str | None
    cases: list[EvalCase]


def load_eval_spec(spec_path: str | Path) -> EvalSpec:
    path = Path(spec_path)
    if not path.exists():
        raise FileNotFoundError(f"Eval spec not found: {path}")

    text = path.read_text(encoding="utf-8")

    # ── Frontmatter ──────────────────────────────────────────────────────────
    fm_match = re.match(r"^---\n(.*?)\n---\n", text, re.DOTALL)
    if not fm_match:
        raise ValueError(f"Eval spec missing YAML frontmatter: {path}")

    fm: dict = yaml.safe_load(fm_match.group(1)) or {}
    body = text[fm_match.end():]

    if fm.get("type") != "eval":
        raise ValueError(
            f"Eval spec frontmatter must have 'type: eval', got: {fm.get('type')!r}"
        )
    app_dsl_path = fm.get("app")
    if not app_dsl_path:
        raise ValueError("Eval spec missing 'app' in frontmatter")

    cases = _parse_cases(body, path)

    return EvalSpec(
        app_dsl_path=app_dsl_path,
        dsl_root=fm.get("dsl_root"),
        model=fm.get("model"),
        cases=cases,
    )


def _parse_cases(body: str, source: Path) -> list[EvalCase]:
    raw_sections = re.split(r"(?m)^## case:\s*", body)
    cases: list[EvalCase] = []

    for section in raw_sections:
        section = section.strip()
        if not section:
            continue

        lines = section.splitlines()
        case_name = lines[0].strip()
        rest = "\n".join(lines[1:])

        input_match = re.search(r'(?m)^input:\s*["\']?(.*?)["\']?\s*$', rest)
        if not input_match:
            raise ValueError(
                f"Case '{case_name}' in {source} is missing an 'input:' line"
            )
        case_input = input_match.group(1).strip().strip("\"'")

        phase_criteria = _parse_phase_criteria(rest, case_name, source)
        cross_phase = _parse_cross_phase_assertions(rest)
        cases.append(EvalCase(
            name=case_name,
            input=case_input,
            phase_criteria=phase_criteria,
            cross_phase=cross_phase,
        ))

    return cases


def _parse_phase_criteria(text: str, case_name: str, source: Path) -> list[PhaseCriteria]:
    raw = re.split(r"(?m)^### ", text)
    result: list[PhaseCriteria] = []

    for section in raw:
        section = section.strip()
        if not section:
            continue

        lines = section.splitlines()
        header = lines[0].strip().lower()
        rest = "\n".join(lines[1:])

        if header == "final":
            phase: str | None = None
        elif header.startswith("phase:"):
            phase = header.split(":", 1)[1].strip()
        else:
            # cross_phase and other unknown headings skipped here
            continue

        schema_assertions, quality_criteria = _parse_schema_quality(rest)
        if not schema_assertions and not quality_criteria:
            continue

        result.append(PhaseCriteria(phase=phase, schema=schema_assertions, criteria=quality_criteria))

    return result


def _parse_cross_phase_assertions(text: str) -> list[CrossPhaseAssertion]:
    """Parse ### cross_phase section into CrossPhaseAssertion objects."""
    raw = re.split(r"(?m)^### ", text)
    result: list[CrossPhaseAssertion] = []

    for section in raw:
        section = section.strip()
        if not section:
            continue
        lines = section.splitlines()
        if lines[0].strip().lower() != "cross_phase":
            continue
        body = "\n".join(lines[1:])
        for line in re.findall(r"(?m)^[-*]\s+(.+)$", body):
            line = line.strip()
            # Format: phase_a.path_a == phase_b.path_b
            m = re.match(
                r'^([\w]+)\.([\w.]+)\s*==\s*([\w]+)\.([\w.]+)$', line
            )
            if m:
                result.append(CrossPhaseAssertion(
                    phase_a=m.group(1), path_a=m.group(2),
                    op="==",
                    phase_b=m.group(3), path_b=m.group(4),
                    raw=line,
                ))

    return result


def _parse_quality_criterion(text: str) -> QualityCriterion:
    """Parse optional [required]/[aspirational] tag prefix from a quality criterion line."""
    import re as _re
    m = _re.match(r'^\[(required|aspirational)\]\s+(.+)$', text, _re.IGNORECASE)
    if m:
        return QualityCriterion(text=m.group(2).strip(), tag=m.group(1).lower())
    return QualityCriterion(text=text, tag="required")


def _parse_schema_quality(text: str) -> tuple[dict | None, list[QualityCriterion]]:
    """Split a phase block into a JSON Schema dict and quality criteria."""
    schema: dict | None = None
    quality_criteria: list[QualityCriterion] = []

    schema_match = re.search(r"(?m)^schema:\s*\n", text)
    quality_match = re.search(r"(?m)^quality:\s*$", text)

    if schema_match or quality_match:
        schema_start = schema_match.start() if schema_match else None
        quality_start = quality_match.start() if quality_match else None

        if schema_match:
            end = quality_start if (quality_start is not None and quality_start > schema_match.end()) else len(text)
            block = text[schema_match.end():end]
            if block.strip():
                try:
                    parsed = yaml.safe_load(block)
                    if isinstance(parsed, dict):
                        schema = parsed
                except yaml.YAMLError:
                    pass

        if quality_match:
            end2 = schema_start if (schema_start is not None and schema_start > quality_match.end()) else len(text)
            quality_block = text[quality_match.end():end2]
            for line in re.findall(r"(?m)^[-*]\s+(.+)$", quality_block):
                quality_criteria.append(_parse_quality_criterion(line.strip()))
    else:
        # Old format: all bullets are quality criteria (required by default)
        for line in re.findall(r"(?m)^[-*]\s+(.+)$", text):
            quality_criteria.append(_parse_quality_criterion(line.strip()))

    return schema, quality_criteria
