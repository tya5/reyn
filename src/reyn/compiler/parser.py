from pathlib import Path

import yaml

from .ir import ArtifactDef, PhaseDef, SkillDef, SkillNodeDef


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

    if "permissions" in fm:
        raise ValueError(
            f"Phase '{name}': phase-level 'permissions:' was removed; "
            f"declare permissions at the skill.md frontmatter instead. "
            f"See docs/reference/dsl/skill-md.md"
        )
    preprocessor_raw = fm.get("preprocessor") or []
    if not isinstance(preprocessor_raw, list):
        raise ValueError(
            f"Phase '{name}': 'preprocessor' must be a YAML list, got {type(preprocessor_raw).__name__}"
        )
    # allowed_ops: distinguish "key absent" (None → expander applies default)
    # from "explicit empty list" (no ops permitted).
    if "allowed_ops" in fm:
        ao_raw = fm.get("allowed_ops")
        if not isinstance(ao_raw, list):
            raise ValueError(
                f"Phase '{name}': 'allowed_ops' must be a YAML list, "
                f"got {type(ao_raw).__name__}"
            )
        allowed_ops: list[str] | None = [str(x).strip() for x in ao_raw if str(x).strip()]
    else:
        allowed_ops = None
    # default_sandbox_policy: optional dict of SandboxPolicy kwargs that the OS
    # applies to every sandboxed_exec op in this phase, winning over the op's own
    # fields (FP-0008 #1115 Stage 2 D mechanism). Absent → None (op fields used).
    if "default_sandbox_policy" in fm:
        dsp_raw = fm.get("default_sandbox_policy")
        if dsp_raw is not None and not isinstance(dsp_raw, dict):
            raise ValueError(
                f"Phase '{name}': 'default_sandbox_policy' must be a YAML mapping, "
                f"got {type(dsp_raw).__name__}"
            )
        default_sandbox_policy: dict | None = dict(dsp_raw) if dsp_raw else None
    else:
        default_sandbox_policy = None
    return PhaseDef(
        name=name,
        inputs=inputs,
        role=fm.get("role") or None,
        can_finish=bool(fm.get("can_finish", False)),
        instructions=body,
        max_act_turns=int(fm.get("max_act_turns", 0)),
        model_class=str(fm.get("model_class") or "").strip(),
        preprocessor=list(preprocessor_raw),
        allowed_ops=allowed_ops,
        default_sandbox_policy=default_sandbox_policy,
    )


import re as _re

_APP_NODE_RE = _re.compile(r'^@([\w]+)(?:\[(isolated|shared)\])?$')


def _parse_graph_node(token: str) -> tuple[str, "SkillNodeDef | None"]:
    """Return (node_id, SkillNodeDef) for @skill_name tokens, or (token, None) for phases."""
    m = _APP_NODE_RE.match(token)
    if not m:
        return token, None
    skill_name = m.group(1)
    workspace = m.group(2) or "isolated"
    return f"@{skill_name}", SkillNodeDef(skill_name=skill_name, workspace=workspace)


def parse_skill(path: Path) -> SkillDef:
    fm, body = _split_frontmatter(path.read_text(encoding="utf-8"))

    edges: list[tuple[str, str]] = []
    skill_nodes: dict[str, SkillNodeDef] = {}

    graph_raw = fm.get("graph") or {}
    for src_raw, targets_raw in graph_raw.items():
        src_id, src_node = _parse_graph_node(str(src_raw))
        if src_node and src_id not in skill_nodes:
            skill_nodes[src_id] = src_node
        if isinstance(targets_raw, str):
            targets_raw = [targets_raw]
        for dst_raw in (targets_raw or []):
            dst_id, dst_node = _parse_graph_node(str(dst_raw))
            if dst_node and dst_id not in skill_nodes:
                skill_nodes[dst_id] = dst_node
            edges.append((src_id, dst_id))

    fc_raw = fm.get("finish_criteria", [])
    if isinstance(fc_raw, str):
        finish_criteria = [c.strip() for c in fc_raw.split(",") if c.strip()]
    else:
        finish_criteria = list(fc_raw)

    postprocessor_raw = fm.get("postprocessor") or {}
    if not isinstance(postprocessor_raw, dict):
        raise ValueError(
            f"skill.md '{path}': 'postprocessor' must be a mapping, got "
            f"{type(postprocessor_raw).__name__}"
        )

    permissions_raw = fm.get("permissions") or {}
    if not isinstance(permissions_raw, dict):
        raise ValueError(
            f"skill.md '{path}': 'permissions' must be a mapping, got "
            f"{type(permissions_raw).__name__}"
        )

    search_hints_raw = fm.get("search_hints", [])
    if isinstance(search_hints_raw, list):
        search_hints = [str(h).strip() for h in search_hints_raw if str(h).strip()]
    elif search_hints_raw is None:
        search_hints = []
    else:
        raise ValueError(
            f"skill.md '{path}': 'search_hints' must be a list of strings, got "
            f"{type(search_hints_raw).__name__}"
        )

    # FP-0016 Component D: required_credentials — optional field.
    # None = omitted (expander will apply ["*"] default).
    # [] = explicitly no credentials.
    # ["key1", ...] = scoped list.
    if "required_credentials" in fm:
        rc_raw = fm["required_credentials"]
        if not isinstance(rc_raw, list):
            raise ValueError(
                f"skill.md '{path}': 'required_credentials' must be a list of strings, "
                f"got {type(rc_raw).__name__}. "
                f"Use a YAML list (e.g. [github_token, openai_key]) or [] for no credentials, "
                f'or omit the field entirely for full delegation (["*"]).'
            )
        required_credentials: list[str] | None = [str(k).strip() for k in rc_raw]
    else:
        required_credentials = None

    return SkillDef(
        name=fm["name"],
        description=str(fm.get("description") or "").strip(),
        doc=body,
        entry=fm["entry"],
        edges=edges,
        skill_nodes=skill_nodes,
        final_output=fm.get("final_output", ""),
        final_output_description=str(fm.get("final_output_description") or "").strip(),
        finish_criteria=finish_criteria,
        postprocessor=postprocessor_raw,
        permissions=permissions_raw,
        search_hints=search_hints,
        required_credentials=required_credentials,
    )
