"""memory ToolDefinitions — Wave 2 of M3 (ADR-0026) + Type C closure.

Five capabilities migrated from chat/router_tools.py ToolSpec literals:
  LIST_MEMORY        — purity=read_only,  category=memory
  READ_MEMORY_BODY   — purity=read_only,  category=memory
  REMEMBER_SHARED    — purity=side_effect, category=memory
  REMEMBER_AGENT     — purity=side_effect, category=memory
  FORGET_MEMORY      — purity=side_effect, category=memory

All five carry gates(router="allow", phase="allow") — this is the
Type C closure for memory write (ADR-0026 §1, §3): the capabilities
were previously router-only; setting phase="allow" closes the gap so
phase-side Control IR can invoke them once M4 wires the phase dispatch
path to consume the registry.

Phase-side dispatch wiring deferred to M4. ToolDefinitions are
registered and gates are open, but the ControlIRExecutor does not yet
consume the registry; M4 closes this final step.

MemoryService access path — design-revisit finding:
  ToolContext.workspace is a reyn.workspace.Workspace instance which
  does NOT carry a MemoryService attribute. MemoryService lives on the
  ChatSession / RouterHostAdapter layer (constructed per-session with
  injected file-op callbacks). The handlers below duplicate the
  router_loop.py logic directly against workspace file primitives —
  they use ctx.workspace.read_file / write_file / delete_file — rather
  than routing through MemoryService. This is the correct short-term
  strategy during M3; M4 cleanup should either:
    (a) surface MemoryService (or equivalent callbacks) on ToolContext, or
    (b) inline workspace-level file ops as the canonical implementation
        and remove the MemoryService indirection.
  Until M4 this duplication is intentional and documented here.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Mapping

from reyn.tools.types import ToolDefinition, ToolGates, ToolContext, ToolResult


# ── Description literals — byte-identical to router_tools.py ToolSpec literals ─

_LIST_MEMORY_DESCRIPTION = (
    'Browse persisted memory hierarchically. Path = "" (roots) '
    '| "shared" | "shared/user" | "agent/feedback" etc. '
    "Returns child categories or item entries "
    "(slug + name + one-line description)."
)

_READ_MEMORY_BODY_DESCRIPTION = (
    "Fetch the full body of one memory entry. "
    "Use only when list_memory's description is too vague "
    "to answer the user."
)

_REMEMBER_SHARED_DESCRIPTION = (
    "Persist a durable fact to project-wide (shared) memory. "
    "Use for user role / project decisions / external references "
    "that benefit all agents."
)

_REMEMBER_AGENT_DESCRIPTION = (
    "Persist a durable fact to this agent's private memory. "
    "Use for agent-specific preferences, feedback, or context "
    "that should not propagate to all agents."
)

_FORGET_MEMORY_DESCRIPTION = (
    "Delete a memory entry. "
    "Only when the user explicitly says 'forget' or "
    "the memory turned out wrong."
)


# ── Parameter schemas — byte-identical to router_tools.py ToolSpec literals ────

_LIST_MEMORY_PARAMETERS: dict[str, Any] = {
    "type": "object",
    "properties": {
        "path": {"type": "string"},
    },
    "required": ["path"],
}

_READ_MEMORY_BODY_PARAMETERS: dict[str, Any] = {
    "type": "object",
    "properties": {
        "layer": {
            "type": "string",
            "enum": ["shared", "agent"],
        },
        "slug": {"type": "string"},
    },
    "required": ["layer", "slug"],
}

_REMEMBER_SHARED_PARAMETERS: dict[str, Any] = {
    "type": "object",
    "properties": {
        "slug": {
            "type": "string",
            "description": (
                "Filename stem, format <type>_<topic>, "
                "e.g. user_role"
            ),
        },
        "name": {"type": "string"},
        "description": {
            "type": "string",
            "description": (
                "One-line summary; appears in memory listings"
            ),
        },
        "type": {
            "type": "string",
            "enum": ["user", "feedback", "project", "reference"],
        },
        "body": {
            "type": "string",
            "description": (
                "Full body markdown, typically <5 lines"
            ),
        },
    },
    "required": ["slug", "name", "description", "type", "body"],
}

_REMEMBER_AGENT_PARAMETERS: dict[str, Any] = {
    "type": "object",
    "properties": {
        "slug": {
            "type": "string",
            "description": (
                "Filename stem, format <type>_<topic>, "
                "e.g. feedback_tone"
            ),
        },
        "name": {"type": "string"},
        "description": {
            "type": "string",
            "description": (
                "One-line summary; appears in memory listings"
            ),
        },
        "type": {
            "type": "string",
            "enum": ["user", "feedback", "project", "reference"],
        },
        "body": {
            "type": "string",
            "description": (
                "Full body markdown, typically <5 lines"
            ),
        },
    },
    "required": ["slug", "name", "description", "type", "body"],
}

_FORGET_MEMORY_PARAMETERS: dict[str, Any] = {
    "type": "object",
    "properties": {
        "layer": {
            "type": "string",
            "enum": ["shared", "agent"],
        },
        "slug": {"type": "string"},
    },
    "required": ["layer", "slug"],
}


# ── Internal helpers ─────────────────────────────────────────────────────────────

def _memory_dir(workspace_base: Path, state_dir: Path, layer: str) -> Path:
    """Resolve memory directory from layer.

    layer="shared" → <cwd>/.reyn/memory
    layer="agent"  → not resolved here (agent dir unknown without ToolContext
                     phase_state; returns state_dir/memory as fallback).

    Design-revisit (M4): phase_state should carry agent_workspace_dir so the
    "agent" layer resolves correctly for phase-side callers. Router-side callers
    receive this via ctx.router_state (not yet populated in M3). For now the
    agent layer always resolves relative to .reyn/agents/ using the agent name
    from phase_state or router_state if available, falling back to state_dir.
    """
    if layer == "shared":
        return state_dir / "memory"
    # agent layer — try to find the agent name from caller context
    # (deferred: see module docstring). Use state_dir/memory as safe fallback.
    return state_dir / "agents" / "memory"


def _resolve_memory_paths(
    ctx: ToolContext,
    layer: str,
    slug: str | None = None,
) -> tuple[Path, Path | None]:
    """Return (mem_dir, body_path_or_None) for the given layer + optional slug.

    Uses workspace.state_dir to construct the path hierarchy.
    phase_state / router_state not yet carrying agent_workspace_dir — see
    module-level design-revisit note.
    """
    state_dir = ctx.workspace.state_dir
    mem_dir = _memory_dir(ctx.workspace.base_dir, state_dir, layer)
    body_path = (mem_dir / f"{slug}.md") if slug is not None else None
    return mem_dir, body_path


def _strip_frontmatter(content: str) -> str:
    """Remove leading YAML frontmatter block from a memory file's text.

    Ported from chat/router_loop.py._strip_frontmatter — same logic, same
    behaviour, same rationale (G12 empty-stop attractor fix). Returns the
    body text without the ---\\n...\\n--- preamble. If no valid frontmatter
    is detected, returns the original text unchanged.
    """
    text = content or ""
    if not text.lstrip().startswith("---"):
        return text
    lines = text.split("\n")
    i = 0
    while i < len(lines) and not lines[i].strip():
        i += 1
    if i >= len(lines) or lines[i].strip() != "---":
        return text
    close = -1
    for j in range(i + 1, len(lines)):
        if lines[j].strip() == "---":
            close = j
            break
    if close == -1:
        return text
    body_lines = lines[close + 1:]
    if body_lines and body_lines[0].strip() == "":
        body_lines = body_lines[1:]
    return "\n".join(body_lines).rstrip("\n") + ("\n" if body_lines else "")


def _parse_memory_index(content: str) -> list[dict]:
    """Parse MEMORY.md content into a flat list of entry dicts.

    Each entry: {layer, slug, name, description}.
    Supports the "# Memory Index (shared)" / "# Memory Index (agent:<name>)"
    section headers written by MemoryService.
    """
    section_re = re.compile(
        r"^#\s+Memory Index\s*(?:\((?P<layer>shared|agent:[^)]*)\))?"
    )
    entry_re = re.compile(r"\[([^\]]+)\]\(([^)]+)\.md\)(?:\s*[—–-]+\s*(.+))?")
    entries: list[dict] = []
    current_layer: str | None = None

    for line in content.splitlines():
        m = section_re.match(line.strip())
        if m:
            layer_raw = m.group("layer") or ""
            if layer_raw == "shared":
                current_layer = "shared"
            elif layer_raw.startswith("agent:"):
                current_layer = "agent"
            else:
                current_layer = None
            continue
        if current_layer is None:
            continue
        for em in entry_re.finditer(line):
            entries.append({
                "layer": current_layer,
                "slug": em.group(2),
                "name": em.group(1),
                "description": (em.group(3) or "").strip(),
            })
    return entries


# ── Handlers ─────────────────────────────────────────────────────────────────────

async def _handle_list_memory(
    args: Mapping[str, Any], ctx: ToolContext
) -> ToolResult:
    """Adapter for list_memory.

    Router path (= production, ADR-0026 Phase 3.5-B-heavy): delegate to
    ``ctx.router_state.list_memory_fn`` which is RouterLoop-bound to
    ``RouterLoop._list_memory``.  That helper consumes
    ``host.get_memory_index()`` (= the agent-aware combined index built
    by the session layer), matching the legacy router branch behavior.

    Fallback (= phase-side / test sites): read the layer indexes directly
    from the workspace filesystem.  This path is NOT agent-aware and is
    intended for non-router callers; the router production path always
    populates list_memory_fn.
    """
    rs = ctx.router_state
    if rs is not None and rs.list_memory_fn is not None:
        return rs.list_memory_fn(args.get("path", ""))

    path = args.get("path", "")
    state_dir = ctx.workspace.state_dir

    def _read_index(layer: str) -> str:
        idx_path = state_dir / ("memory" if layer == "shared" else "agents/memory") / "MEMORY.md"
        try:
            content, found = ctx.workspace.read_file(str(idx_path))
            return content if found else ""
        except Exception:
            return ""

    shared_content = _read_index("shared")
    agent_content = _read_index("agent")

    if not path:
        # Return root: [{path: "shared", count: N}, {path: "agent", count: M}]
        shared_entries = _parse_memory_index(shared_content)
        agent_entries = _parse_memory_index(agent_content)
        shared_count = sum(1 for e in shared_entries if e["layer"] == "shared")
        agent_count = sum(1 for e in agent_entries if e["layer"] == "agent")
        return [
            {"path": "shared", "count": shared_count},
            {"path": "agent", "count": agent_count},
        ]

    parts = path.split("/", 1)
    layer = parts[0]
    content = shared_content if layer == "shared" else agent_content
    all_entries = _parse_memory_index(content)
    layer_entries = [e for e in all_entries if e["layer"] == layer]

    if len(parts) == 1:
        # Return sub-categories (types) for this layer
        type_counts: dict[str, int] = {}
        type_re = re.compile(r"^(user|feedback|project|reference)_")
        for e in layer_entries:
            tm = type_re.match(e["slug"])
            if tm:
                mtype = tm.group(1)
                type_counts[mtype] = type_counts.get(mtype, 0) + 1
        return [
            {"path": f"{layer}/{mtype}", "count": count}
            for mtype, count in sorted(type_counts.items())
            if count > 0
        ]

    # path == "shared/user" etc. → items matching layer + type
    mtype = parts[1]
    type_re = re.compile(r"^(user|feedback|project|reference)_")
    items = []
    for e in layer_entries:
        tm = type_re.match(e["slug"])
        if tm and tm.group(1) == mtype:
            items.append({
                "slug": e["slug"],
                "name": e["name"],
                "description": e["description"],
            })
    return items


async def _handle_read_memory_body(
    args: Mapping[str, Any], ctx: ToolContext
) -> ToolResult:
    """Adapter for read_memory_body.

    Router path: delegate to ``ctx.router_state.read_memory_body_fn``
    (= RouterLoop._read_memory_body) which uses ``host.memory_path`` +
    ``host.file_read`` for agent-aware file resolution.

    Fallback: read the body file via ``ctx.workspace.read_file`` and
    strip the YAML frontmatter (= same G12 attractor fix logic as the
    router helper).
    """
    rs = ctx.router_state
    if rs is not None and rs.read_memory_body_fn is not None:
        return await rs.read_memory_body_fn(
            args.get("layer", ""), args.get("slug", ""),
        )

    layer = args.get("layer", "")
    slug = args.get("slug", "")

    _mem_dir, body_path = _resolve_memory_paths(ctx, layer, slug)
    if body_path is None:
        return {"error": "slug is required"}

    try:
        content, found = ctx.workspace.read_file(str(body_path))
        if not found:
            return {"error": f"memory entry not found: {slug}", "layer": layer, "slug": slug}
        return {
            "content": _strip_frontmatter(content),
            "layer": layer,
            "slug": slug,
        }
    except Exception as exc:
        return {"error": str(exc), "layer": layer, "slug": slug}


async def _handle_remember(
    args: Mapping[str, Any],
    ctx: ToolContext,
    *,
    layer: str,
) -> ToolResult:
    """Shared adapter body for remember_shared / remember_agent.

    Router path: delegate to ``ctx.router_state.remember_fn``
    (= RouterLoop._remember) which uses ``host.memory_path`` +
    ``host.file_write`` + ``host.file_regenerate_index`` for atomic
    write + index regen (= the same multi-step sequence the legacy
    router branch performed).

    Fallback: write frontmatter + body via ctx.workspace, then
    regenerate MEMORY.md by scanning the layer dir.
    """
    rs = ctx.router_state
    if rs is not None and rs.remember_fn is not None:
        return await rs.remember_fn(
            layer=layer,
            slug=args.get("slug", ""),
            name=args.get("name", ""),
            description=args.get("description", ""),
            type=args.get("type", ""),
            body=args.get("body", ""),
        )

    slug = args.get("slug", "")
    name = args.get("name", "")
    description = args.get("description", "")
    mem_type = args.get("type", "")
    body = args.get("body", "")

    # Defensive: strip trailing .md (LLM may emit it despite the schema description).
    if slug.endswith(".md"):
        slug = slug[:-3]

    mem_dir, body_path = _resolve_memory_paths(ctx, layer, slug)
    if body_path is None:
        return {"error": "slug is required"}

    frontmatter = (
        f"---\nname: {name}\ndescription: {description}\ntype: {mem_type}\n---\n\n{body}\n"
    )

    try:
        ctx.workspace.write_file(str(body_path), frontmatter)
    except Exception as exc:
        return {"error": str(exc)}

    # Regenerate MEMORY.md index — scan the layer dir and rebuild.
    index_path = mem_dir / "MEMORY.md"
    try:
        _regenerate_index(ctx, mem_dir, index_path)
    except Exception as exc:
        return {"error": f"index regeneration failed: {exc}"}

    ctx.events.emit("memory_saved", layer=layer, slug=slug, path=str(body_path))
    return {"saved": slug, "layer": layer, "path": str(body_path)}


async def _handle_remember_shared(
    args: Mapping[str, Any], ctx: ToolContext
) -> ToolResult:
    """Adapter for remember_shared — delegates to shared layer."""
    return await _handle_remember(args, ctx, layer="shared")


async def _handle_remember_agent(
    args: Mapping[str, Any], ctx: ToolContext
) -> ToolResult:
    """Adapter for remember_agent — delegates to agent layer."""
    return await _handle_remember(args, ctx, layer="agent")


async def _handle_forget_memory(
    args: Mapping[str, Any], ctx: ToolContext
) -> ToolResult:
    """Adapter for forget_memory.

    Router path: delegate to ``ctx.router_state.forget_fn``
    (= RouterLoop._forget) which uses ``host.memory_path`` +
    ``host.file_delete`` + ``host.file_regenerate_index`` matching the
    legacy router branch.

    Fallback: delete the body file via ctx.workspace and regenerate the
    layer's MEMORY.md.
    """
    rs = ctx.router_state
    if rs is not None and rs.forget_fn is not None:
        return await rs.forget_fn(args.get("layer", ""), args.get("slug", ""))

    layer = args.get("layer", "")
    slug = args.get("slug", "")

    if slug.endswith(".md"):
        slug = slug[:-3]

    mem_dir, body_path = _resolve_memory_paths(ctx, layer, slug)
    if body_path is None:
        return {"error": "slug is required"}

    try:
        deleted = ctx.workspace.delete_file(str(body_path))
    except Exception as exc:
        return {"error": str(exc)}

    if not deleted:
        return {"error": f"memory entry not found: {slug}"}

    index_path = mem_dir / "MEMORY.md"
    try:
        _regenerate_index(ctx, mem_dir, index_path)
    except Exception as exc:
        return {"error": f"index regeneration failed: {exc}"}

    ctx.events.emit("memory_deleted", layer=layer, slug=slug, path=str(body_path))
    return {"deleted": slug, "layer": layer}


# ── Index regeneration helper ────────────────────────────────────────────────────

def _regenerate_index(ctx: ToolContext, mem_dir: Path, index_path: Path) -> None:
    """Regenerate MEMORY.md by scanning <mem_dir>/*.md (excluding MEMORY.md).

    Reads frontmatter from each .md file to extract name / description, then
    writes a fresh MEMORY.md. This is a synchronous filesystem operation —
    acceptable for M3 because workspace file ops are synchronous. M4 may switch
    to the MemoryService async callbacks when ToolContext exposes them.
    """
    frontmatter_re = re.compile(
        r"^---\s*\nname:\s*(?P<name>[^\n]*)\ndescription:\s*(?P<desc>[^\n]*)\n",
        re.MULTILINE,
    )

    entries: list[tuple[str, str, str]] = []  # (slug, name, description)
    try:
        mem_dir.mkdir(parents=True, exist_ok=True)
        for md_file in sorted(mem_dir.glob("*.md")):
            if md_file.name == "MEMORY.md":
                continue
            try:
                content, _ = ctx.workspace.read_file(str(md_file))
                m = frontmatter_re.search(content)
                name_val = m.group("name").strip() if m else md_file.stem
                desc_val = m.group("desc").strip() if m else ""
            except Exception:
                name_val = md_file.stem
                desc_val = ""
            entries.append((md_file.stem, name_val, desc_val))
    except Exception:
        pass

    lines = ["# Memory Index\n\n"]
    for slug, name, desc in entries:
        lines.append(f"- [{name}]({slug}.md) — {desc}\n")

    ctx.workspace.write_file(str(index_path), "".join(lines))


# ── ToolDefinition instances ─────────────────────────────────────────────────────

LIST_MEMORY = ToolDefinition(
    name="list_memory",
    description=_LIST_MEMORY_DESCRIPTION,
    parameters=_LIST_MEMORY_PARAMETERS,
    gates=ToolGates(router="allow", phase="allow"),  # Type C closure
    handler=_handle_list_memory,
    category="memory",
    purity="read_only",
)

READ_MEMORY_BODY = ToolDefinition(
    name="read_memory_body",
    description=_READ_MEMORY_BODY_DESCRIPTION,
    parameters=_READ_MEMORY_BODY_PARAMETERS,
    gates=ToolGates(router="allow", phase="allow"),  # Type C closure
    handler=_handle_read_memory_body,
    category="memory",
    purity="read_only",
)

REMEMBER_SHARED = ToolDefinition(
    name="remember_shared",
    description=_REMEMBER_SHARED_DESCRIPTION,
    parameters=_REMEMBER_SHARED_PARAMETERS,
    gates=ToolGates(router="allow", phase="allow"),  # Type C closure
    handler=_handle_remember_shared,
    category="memory",
    purity="side_effect",
)

REMEMBER_AGENT = ToolDefinition(
    name="remember_agent",
    description=_REMEMBER_AGENT_DESCRIPTION,
    parameters=_REMEMBER_AGENT_PARAMETERS,
    gates=ToolGates(router="allow", phase="allow"),  # Type C closure
    handler=_handle_remember_agent,
    category="memory",
    purity="side_effect",
)

FORGET_MEMORY = ToolDefinition(
    name="forget_memory",
    description=_FORGET_MEMORY_DESCRIPTION,
    parameters=_FORGET_MEMORY_PARAMETERS,
    gates=ToolGates(router="allow", phase="allow"),  # Type C closure
    handler=_handle_forget_memory,
    category="memory",
    purity="side_effect",
)
