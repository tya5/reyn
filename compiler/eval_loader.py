"""
Parser for eval.md spec files.

Format:
  ---
  type: eval
  app: dsl/apps/foo/app.md
  dsl_root: dsl/
  model: gpt-4o
  judge_model: gpt-4o
  ---

  ## case: my_case
  input: "the test input text"

  ### phase: analyze_code
  schema:
  - field_name: string
  - score: number, range 0.0-1.0
  - label: string, equals "approved"
  - body: string, contains "asyncio"
  - items: array, min 1

  quality:
  - criterion one
  - criterion two

  ### cross_phase
  - write_memo.filename == read_verify.filename

  ### final
  schema:
  - output_field: array

  quality:
  - criterion three
"""
from __future__ import annotations
import re
from pathlib import Path

import yaml

from agent_os.eval_models import (
    EvalSpec, EvalCase, PhaseCriteria, SchemaAssertion, CrossPhaseAssertion,
    QualityCriterion,
)


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
        judge_model=fm.get("judge_model"),
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


def _parse_schema_quality(text: str) -> tuple[list[SchemaAssertion], list[QualityCriterion]]:
    """Split a phase block into schema assertions and quality criteria."""
    schema_assertions: list[SchemaAssertion] = []
    quality_criteria: list[str] = []

    schema_match = re.search(r"(?m)^schema:\s*$", text)
    quality_match = re.search(r"(?m)^quality:\s*$", text)

    if schema_match or quality_match:
        schema_start = schema_match.start() if schema_match else None
        quality_start = quality_match.start() if quality_match else None

        if schema_start is not None:
            end = quality_start if (quality_start is not None and quality_start > schema_start) else len(text)
            schema_block = text[schema_match.end():end]
            for line in re.findall(r"(?m)^[-*]\s+(.+)$", schema_block):
                sa = _parse_schema_assertion(line.strip())
                if sa:
                    schema_assertions.append(sa)

        if quality_start is not None:
            end2 = schema_start if (schema_start is not None and schema_start > quality_start) else len(text)
            quality_block = text[quality_match.end():end2]
            for line in re.findall(r"(?m)^[-*]\s+(.+)$", quality_block):
                quality_criteria.append(_parse_quality_criterion(line.strip()))
    else:
        # Old format: all bullets are quality criteria (required by default)
        for line in re.findall(r"(?m)^[-*]\s+(.+)$", text):
            quality_criteria.append(_parse_quality_criterion(line.strip()))

    return schema_assertions, quality_criteria


def _parse_schema_assertion(raw: str) -> SchemaAssertion | None:
    """Parse a single schema assertion line.

    Supported constraints:
      range 0.0-1.0          numeric range (inclusive)
      min <n>                 numeric minimum, or minimum array length
      max <n>                 numeric maximum, or maximum array length
      min_length <n>          string/array minimum length
      max_length <n>          string/array maximum length
      equals <value>          exact value match (string, number, boolean)
      contains <text>         substring (string) or any-element-contains (array)

    Examples:
      filename: string, equals "daily_report.txt"
      score: number, range 0.0-1.0
      issues: array, min 1
      body: string, contains "asyncio"
      verified: boolean, equals true
    """
    if ":" not in raw:
        return None

    path_part, rest = raw.split(":", 1)
    path = path_part.strip()
    rest = rest.strip()

    parts = [p.strip() for p in rest.split(",")]
    if not parts or not parts[0]:
        return None

    type_str = parts[0].lower()
    valid_types = {"string", "number", "integer", "boolean", "array", "object"}
    if type_str not in valid_types:
        return None

    constraints: dict = {}
    for part in parts[1:]:
        part = part.strip()
        if not part:
            continue

        # range 0.0-1.0
        m = re.match(r"^range\s+([\d.]+)-([\d.]+)$", part)
        if m:
            constraints["range"] = (float(m.group(1)), float(m.group(2)))
            continue

        # min <n> / max <n>
        m = re.match(r"^(min|max)\s+([\d.]+)$", part)
        if m:
            constraints[m.group(1)] = float(m.group(2))
            continue

        # min_length <n> / max_length <n>
        m = re.match(r"^(min_length|max_length)\s+(\d+)$", part)
        if m:
            constraints[m.group(1)] = int(m.group(2))
            continue

        # equals "quoted string"
        m = re.match(r'^equals\s+"([^"]*)"$', part)
        if m:
            constraints["equals"] = m.group(1)
            continue

        # equals unquoted value (true/false/number)
        m = re.match(r"^equals\s+(\S+)$", part)
        if m:
            val = m.group(1)
            if val == "true":
                constraints["equals"] = True
            elif val == "false":
                constraints["equals"] = False
            else:
                try:
                    constraints["equals"] = float(val) if "." in val else int(val)
                except ValueError:
                    constraints["equals"] = val
            continue

        # contains "quoted string"
        m = re.match(r'^contains\s+"([^"]*)"$', part)
        if m:
            constraints["contains"] = m.group(1)
            continue

        # contains unquoted token
        m = re.match(r"^contains\s+(\S+)$", part)
        if m:
            constraints["contains"] = m.group(1)
            continue

    return SchemaAssertion(path=path, type=type_str, constraints=constraints, raw=raw)
