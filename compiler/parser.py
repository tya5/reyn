import yaml
from pathlib import Path
from .ir import FieldDef, ArtifactDef, PhaseDef, AppDef


def _split_frontmatter(text: str) -> tuple[dict, str]:
    """Split a Markdown file into (frontmatter dict, body string)."""
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}, text
    end = next((i for i, l in enumerate(lines[1:], 1) if l.strip() == "---"), None)
    if end is None:
        return {}, text
    fm = yaml.safe_load("\n".join(lines[1:end])) or {}
    body = "\n".join(lines[end + 1:]).strip()
    return fm, body


def _parse_fields(body: str) -> list[FieldDef]:
    """
    Parse field definitions from an artifact body via YAML.

    Simple primitive type (string value):
      name: string        # required
      name?: integer      # optional
      tags: string[]

    Inline JSON Schema (dict value — any valid JSON Schema):
      scores:
        type: array
        items:
          type: number

      metadata?:
        type: object
        properties:
          key: {type: string}
          value: {type: string}
        required: [key]
    """
    data = yaml.safe_load(body)
    if not data or not isinstance(data, dict):
        return []
    fields = []
    for raw_key, value in data.items():
        raw_key = str(raw_key)
        optional = raw_key.endswith("?")
        name = raw_key[:-1] if optional else raw_key
        if isinstance(value, dict):
            fields.append(FieldDef(name=name, type_str="object", optional=optional, schema=value))
        else:
            fields.append(FieldDef(name=name, type_str=str(value).strip(), optional=optional))
    return fields


def parse_artifact(path: Path) -> ArtifactDef:
    fm, body = _split_frontmatter(path.read_text(encoding="utf-8"))
    return ArtifactDef(
        name=fm["name"],
        fields=_parse_fields(body),
        wrapped=bool(fm.get("wrapped", True)),
    )


def parse_phase(path: Path) -> PhaseDef:
    fm, body = _split_frontmatter(path.read_text(encoding="utf-8"))
    name = fm.get("name", path.stem)

    if "output" in fm:
        raise ValueError(
            f"Phase '{name}' must not define output. "
            "Output schema is provided at runtime from candidate next phase input schemas "
            "or app final output schema."
        )

    inputs_raw = fm.get("input", "")
    inputs = [i.strip() for i in str(inputs_raw).split("|")] if inputs_raw else []
    input_description = str(fm.get("input_description") or "").strip()

    return PhaseDef(
        name=name,
        inputs=inputs,
        input_description=input_description,
        role=fm.get("role") or None,
        can_finish=bool(fm.get("can_finish", False)),
        instructions=body,
    )


def parse_app(path: Path) -> AppDef:
    fm, body = _split_frontmatter(path.read_text(encoding="utf-8"))

    # Parse graph: "A -> B -> C" lines into (src, dst) edges
    edges: list[tuple[str, str]] = []
    for line in body.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = [p.strip() for p in line.split("->")]
        for i in range(len(parts) - 1):
            edges.append((parts[i], parts[i + 1]))

    # finish_criteria may be a comma-separated string or a YAML list
    fc_raw = fm.get("finish_criteria", [])
    if isinstance(fc_raw, str):
        finish_criteria = [c.strip() for c in fc_raw.split(",") if c.strip()]
    else:
        finish_criteria = list(fc_raw)

    return AppDef(
        name=fm["name"],
        entry=fm["entry"],
        edges=edges,
        final_output=fm.get("final_output", ""),
        final_output_description=str(fm.get("final_output_description") or "").strip(),
        finish_criteria=finish_criteria,
        max_phase_visits={k: int(v) for k, v in (fm.get("max_phase_visits") or {}).items()},
    )
