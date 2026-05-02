"""System prompt builder for the native tool_use router loop (PR35).

Size is O(categories), independent of item count — Progressive Disclosure
(Lazy Hierarchical Catalog).  Path-level detail is deferred to list_* tools
at runtime.
"""
from __future__ import annotations

import re
from collections import defaultdict


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def build_system_prompt(
    *,
    agent_name: str,
    agent_role: str,
    available_skills: list[dict],
    available_agents: list[dict],
    memory_index: dict,
    file_permissions: dict | None = None,
    mcp_servers: list[dict] | None = None,
) -> str:
    """Render the system prompt for the tool_use router loop.

    Returns text matching the structure in the plan's "System prompt 構成"
    section.  Size is O(categories), independent of item count.

    Args:
        agent_name: short identifier of the agent (e.g. "chat").
        agent_role: one-liner from agent profile.
        available_skills: list of dicts with at least ``name``; optional keys
            ``description``, ``routing``, ``category``.
        available_agents: list of dicts with at least ``name``; optional keys
            ``role``, ``cluster``.
        memory_index: ``{"status": "ok"|"not_found", "content": str}``.
        file_permissions: optional ``{"read": [paths], "write": [paths]}``.
            When non-empty (either list), a Files section is rendered.
        mcp_servers: optional list of ``{"name": ..., "description": ...}``.
            When non-empty, an MCP servers section is rendered.
    """
    skill_section = _render_skills(available_skills)
    agent_section = _render_agents(available_agents)
    memory_section = _render_memory(memory_index)
    file_section = _render_files(file_permissions)
    mcp_section = _render_mcp(mcp_servers)

    parts: list[str] = []

    parts.append(
        f"Role: chat router for agent {agent_name} (role: {agent_role})."
    )
    parts.append("")
    parts.append("## What you can do (intent axis)")
    parts.append("")
    parts.append("- Action — run external work")
    parts.append(
        "           skills:  list_skills / describe_skill / invoke_skill"
    )
    parts.append(
        "           agents:  list_agents / describe_agent / delegate_to_agent"
    )
    parts.append(
        "           files:   list_directory / read_file"
        "        (when file scope set)"
    )
    parts.append(
        "           mcp:     list_mcp_servers / list_mcp_tools"
    )
    parts.append(
        "                    / call_mcp_tool"
        "                   (when mcp configured)"
    )
    parts.append("- Recall — read persisted facts")
    parts.append("           tools: list_memory / read_memory_body")
    parts.append("- Save — persist new facts")
    parts.append("         tools: remember_shared / remember_agent")
    parts.append(
        "                write_file"
        "                          (when file write scope set)"
    )
    parts.append("- Forget — delete persisted facts")
    parts.append("           tools: forget_memory")
    parts.append(
        "                  delete_file"
        "                         (when file write scope set)"
    )
    parts.append("- Reply — answer directly (no tool)")
    parts.append("")
    parts.append(
        "## Skills (resource axis, categories — use list_skills(path) to drill)"
    )
    parts.append(f"  {skill_section}")
    parts.append("")
    parts.append("## Agents (resource axis, clusters)")
    parts.append(f"  {agent_section}")
    parts.append("")
    parts.append(
        "## Memory (resource axis, path roots — use list_memory(path))"
    )
    for line in memory_section:
        parts.append(f"  {line}")
    parts.append("")
    if file_section:
        parts.append("## Files (resource axis — permission-scoped)")
        for line in file_section:
            parts.append(f"  {line}")
        parts.append("")
    if mcp_section:
        parts.append("## MCP servers (resource axis)")
        for line in mcp_section:
            parts.append(f"  {line}")
        parts.append("")
    parts.append("## Behaviour")
    parts.append("  - Match the user's language for any text reply.")
    parts.append(
        "  - First decide intent (Action / Recall / Save / Forget / Reply),"
    )
    parts.append("    then pick tools from that group.")
    parts.append(
        "  - Reply directly (no tools) for chitchat / stable knowledge."
    )
    parts.append(
        "  - For Action, browse the relevant skill category first"
    )
    parts.append(
        "    (list_skills, then describe_skill if needed) before invoke_skill."
    )
    parts.append(
        "  - For Recall, list_memory the relevant path first; read_memory_body"
    )
    parts.append(
        "    only when the listing's description is too vague."
    )
    parts.append(
        "  - Use parallel tool_calls when discovery / fetches are independent."
    )
    parts.append(
        "  - For sequential dependencies, one tool_call per round."
    )
    parts.append(
        "  - Never invent skill / agent / slug names; only use those returned"
    )
    parts.append("    by list_*.")
    parts.append(
        '  - Memory writes (Save) via remember_*. Triggers: "remember", "覚えて",'
    )
    parts.append('    "save", "from now on", "treat as".')

    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _render_skills(available_skills: list[dict]) -> str:
    """Return one-line category summary, or empty string when no skills."""
    counts: dict[str, int] = defaultdict(int)
    for skill in available_skills:
        cat = skill.get("category") or "general"
        counts[cat] += 1

    if not counts:
        return "(none)"

    # Stable sort: alphabetical within category, general first
    ordered = sorted(counts.items(), key=lambda kv: (kv[0] != "general", kv[0]))
    tokens = [f"{cat} ({n})" for cat, n in ordered]
    return " / ".join(tokens)


def _render_agents(available_agents: list[dict]) -> str:
    """Return one-line cluster summary, or empty string when no agents."""
    counts: dict[str, int] = defaultdict(int)
    for agent in available_agents:
        cluster = agent.get("cluster") or "default"
        counts[cluster] += 1

    if not counts:
        return "(none)"

    ordered = sorted(counts.items(), key=lambda kv: (kv[0] != "default", kv[0]))
    tokens = [f"{cluster} ({n} peer{'s' if n != 1 else ''})" for cluster, n in ordered]
    return " / ".join(tokens)


_SLUG_TYPE_RE = re.compile(r"^(user|feedback|project|reference)_")
_MEMORY_TYPES = ("user", "feedback", "project", "reference")

# Matches section headers like "# Memory Index (shared)" or
# "# Memory Index (agent: chat_20240101)"
_SECTION_HEADER_RE = re.compile(
    r"^#\s+Memory Index\s*\((?P<layer>shared|agent:[^)]*)\)"
)
# Matches list entries like "- [Title](slug.md) — description"
# or "| slug | ..."  (table row)
_ENTRY_SLUG_RE = re.compile(r"\(([^)]+)\.md\)")


def _parse_memory_counts(content: str) -> dict[str, dict[str, int]]:
    """Return {layer: {type: count}} from merged memory index text.

    Layers are "shared" and "agent".  Types are user/feedback/project/reference.
    """
    shared: dict[str, int] = defaultdict(int)
    agent: dict[str, int] = defaultdict(int)

    current_bucket: dict[str, int] | None = None

    for line in content.splitlines():
        m = _SECTION_HEADER_RE.match(line.strip())
        if m:
            layer_raw = m.group("layer")
            if layer_raw == "shared":
                current_bucket = shared
            else:  # "agent:…"
                current_bucket = agent
            continue

        if current_bucket is None:
            continue

        # Look for slug references anywhere in the line
        for slug_match in _ENTRY_SLUG_RE.finditer(line):
            slug = slug_match.group(1)
            tm = _SLUG_TYPE_RE.match(slug)
            if tm:
                current_bucket[tm.group(1)] += 1

    return {"shared": dict(shared), "agent": dict(agent)}


def _render_files(file_permissions: dict | None) -> list[str]:
    """Return lines for the Files section, or [] when nothing to render."""
    if not file_permissions:
        return []
    read_paths = file_permissions.get("read") or []
    write_paths = file_permissions.get("write") or []
    if not read_paths and not write_paths:
        return []
    lines: list[str] = []
    if read_paths:
        lines.append(f"read scope:  {', '.join(read_paths)}")
    if write_paths:
        lines.append(f"write scope: {', '.join(write_paths)}")
    return lines


def _render_mcp(mcp_servers: list[dict] | None) -> list[str]:
    """Return lines for the MCP servers section, or [] when nothing to render."""
    if not mcp_servers:
        return []
    lines: list[str] = []
    for server in mcp_servers:
        name = server.get("name", "(unnamed)")
        desc = server.get("description") or "(no description)"
        lines.append(f"- {name}: {desc}  (use list_mcp_tools to see tools)")
    return lines


def _render_memory(memory_index: dict) -> list[str]:
    """Return lines for the memory section."""
    zero = {t: 0 for t in _MEMORY_TYPES}

    if memory_index.get("status") != "ok":
        shared_counts = zero.copy()
        agent_counts = zero.copy()
    else:
        content = memory_index.get("content", "")
        parsed = _parse_memory_counts(content)
        shared_counts = {t: parsed["shared"].get(t, 0) for t in _MEMORY_TYPES}
        agent_counts = {t: parsed["agent"].get(t, 0) for t in _MEMORY_TYPES}

    def _fmt(counts: dict[str, int]) -> str:
        tokens = [f"{t}({counts[t]})" for t in _MEMORY_TYPES if counts[t] > 0]
        return " ".join(tokens) if tokens else "(empty)"

    shared_str = _fmt(shared_counts)
    agent_str = _fmt(agent_counts)

    return [
        f"shared/{{{shared_str}}}",
        f"agent/{{{agent_str}}}",
    ]
