import yaml
from pathlib import Path
from .ir import ArtifactDef, PhaseDef, AppDef, AppNodeDef


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


def parse_artifact(path: Path) -> ArtifactDef:
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return ArtifactDef(
        name=data["name"],
        description=str(data.get("description") or "").strip(),
        schema=data.get("schema") or {},
        wrapped=bool(data.get("wrapped", True)),
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

    permissions_raw = fm.get("permissions") or {}
    return PhaseDef(
        name=name,
        inputs=inputs,
        role=fm.get("role") or None,
        can_finish=bool(fm.get("can_finish", False)),
        instructions=body,
        max_act_turns=int(fm.get("max_act_turns", 0)),
        model_class=str(fm.get("model_class") or "").strip(),
        permissions=permissions_raw if isinstance(permissions_raw, dict) else {},
    )


import re as _re
_APP_NODE_RE = _re.compile(r'^@([\w]+)(?:\[(isolated|shared)\])?$')


def _parse_graph_node(token: str) -> tuple[str, "AppNodeDef | None"]:
    """Return (node_id, AppNodeDef) for @app_name tokens, or (token, None) for phases."""
    m = _APP_NODE_RE.match(token)
    if not m:
        return token, None
    app_name = m.group(1)
    workspace = m.group(2) or "isolated"
    return f"@{app_name}", AppNodeDef(app_name=app_name, workspace=workspace)


def parse_app(path: Path) -> AppDef:
    fm, body = _split_frontmatter(path.read_text(encoding="utf-8"))

    edges: list[tuple[str, str]] = []
    app_nodes: dict[str, AppNodeDef] = {}

    graph_raw = fm.get("graph") or {}
    for src_raw, targets_raw in graph_raw.items():
        src_id, src_node = _parse_graph_node(str(src_raw))
        if src_node and src_id not in app_nodes:
            app_nodes[src_id] = src_node
        if isinstance(targets_raw, str):
            targets_raw = [targets_raw]
        for dst_raw in (targets_raw or []):
            dst_id, dst_node = _parse_graph_node(str(dst_raw))
            if dst_node and dst_id not in app_nodes:
                app_nodes[dst_id] = dst_node
            edges.append((src_id, dst_id))

    fc_raw = fm.get("finish_criteria", [])
    if isinstance(fc_raw, str):
        finish_criteria = [c.strip() for c in fc_raw.split(",") if c.strip()]
    else:
        finish_criteria = list(fc_raw)

    return AppDef(
        name=fm["name"],
        description=str(fm.get("description") or "").strip(),
        doc=body,
        entry=fm["entry"],
        edges=edges,
        app_nodes=app_nodes,
        final_output=fm.get("final_output", ""),
        final_output_description=str(fm.get("final_output_description") or "").strip(),
        finish_criteria=finish_criteria,
        max_phase_visits={k: int(v) for k, v in (fm.get("max_phase_visits") or {}).items()},
    )
