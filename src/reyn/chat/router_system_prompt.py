"""System prompt builder for the native tool_use router loop (PR35).

Size is O(categories), independent of item count — Progressive Disclosure
(Lazy Hierarchical Catalog).  Path-level detail is deferred to list_* tools
at runtime.
"""
from __future__ import annotations

import re
from collections import defaultdict

from reyn.chat.router_tools import MAX_DESC_LEN_FOR_LISTING


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
    output_language: str | None = None,
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
        output_language: BCP-47-style language code (e.g. "ja", "en"),
            or None when unset. When set, the Behaviour section emits a
            strict "Always reply in language: <code>" directive so the
            LLM stays in that language even on clarifying / error paths.
            When None (= user did not configure output_language), no
            language directive is emitted — the LLM picks the reply
            language based on the user's input naturally. This avoids
            forcing a default (= "ja") onto users who haven't expressed
            a preference.
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
    has_file_read = bool(
        file_permissions and file_permissions.get("read")
    )
    has_file_write = bool(
        file_permissions and file_permissions.get("write")
    )
    has_mcp = bool(mcp_servers)

    parts.append("- Action — run external work")
    parts.append(
        "           skills:  list_skills / describe_skill / invoke_skill"
    )
    parts.append(
        "           agents:  list_agents / describe_agent / delegate_to_agent"
    )
    if has_file_read:
        parts.append(
            "           files:   list_directory / read_file"
        )
    if has_mcp:
        parts.append(
            "           mcp:     list_mcp_servers / list_mcp_tools / call_mcp_tool"
        )
    parts.append("- Recall — read persisted facts")
    parts.append("           tools: list_memory / read_memory_body")
    parts.append("- Save — persist new facts")
    if has_file_write:
        parts.append(
            "         tools: remember_shared / remember_agent / write_file"
        )
    else:
        parts.append("         tools: remember_shared / remember_agent")
    parts.append("- Forget — delete persisted facts")
    if has_file_write:
        parts.append("           tools: forget_memory / delete_file")
    else:
        parts.append("           tools: forget_memory")
    parts.append("- Reply — answer directly (no tool)")
    parts.append("")
    # RETRO-H1+H2 fix: inject flat skill list so the LLM knows actual skill
    # names and can't zero-shot hallucinate them. Paired with enum constraint
    # in build_tools (schema layer) for defense in depth (P4).
    skill_count = len(available_skills)
    parts.append(
        f"## Available skills ({skill_count}) — use these exact names with invoke_skill"
    )
    if available_skills:
        for skill in available_skills:
            name = skill.get("name", "")
            raw_desc = skill.get("description") or ""
            # Truncate to MAX_DESC_LEN_FOR_LISTING chars to mitigate the G12
            # empty-stop attractor (B7 finding — Pattern C: system prompt
            # inline skill list verbosity triggers the attractor — a947255e).
            if len(raw_desc) > MAX_DESC_LEN_FOR_LISTING:
                desc = raw_desc[:MAX_DESC_LEN_FOR_LISTING] + "..."
            else:
                desc = raw_desc
            # One-liner per skill: name + description (keeps prompt scannable)
            if desc:
                parts.append(f"  - {name}: {desc}")
            else:
                parts.append(f"  - {name}")
    else:
        parts.append("  (none)")
    parts.append(f"  Categories: {skill_section}")
    parts.append("")
    parts.append("## Agents (resource axis, clusters)")
    parts.append(f"  {agent_section}")
    parts.append("")
    parts.append(
        "## Memory (entries inlined — answer recall queries from these "
        "descriptions; use read_memory_body for full content if vague)"
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
    # Explicit language instruction (only when the user configured one):
    # a concrete language tag is stronger than "match the user's language"
    # — the LLM stays in this language even on clarifying-question and
    # error fallback paths (F11). When output_language is None (= user did
    # not configure), we omit the directive entirely so the LLM can pick
    # the reply language based on the user's input naturally instead of
    # being forced into a Reyn default. (Q2 follow-up to F11 fix.)
    if output_language:
        parts.append(
            f"  - Always reply in language: {output_language}."
            "  Do NOT switch language even for error messages or clarifying questions."
        )
    parts.append(
        "  - First decide intent (Action / Recall / Save / Forget / Reply),"
    )
    parts.append("    then pick tools from that group.")
    # Behaviour rules — re-balanced from B5-H1 partial revert of e90c0f2.
    # History: F3+F9 (batch 1) added reply restriction + explicit-skill hint;
    # B2-H1 (batch 2) added post-describe_skill commit obligation;
    # B3-H1+M3 (batch 3) added post-list_skills commit obligation.
    # e90c0f2 over-consolidated to 2 rules; weak LLM (gemini-2.5-flash-lite)
    # de-prioritised multi-sentence MUSTs inside a single bullet → B5-H1
    # regression (specialist empty reply after list_skills).
    # Fix: restore individual bullets (1 bullet = 1 MUST) per feedback_prompt_design.
    # "engage the skill ecosystem" jargon removed; duplicate list+invoke hints merged.
    #
    # Bullet 1 (F3+F9 — chitchat restriction, domain → Action):
    parts.append(
        "  - Reply directly only for chitchat, questions about yourself,"
    )
    parts.append(
        "    and clarifications back to the user. Domain tasks → Action."
    )
    # Bullet 2 (F3+F9+B3-H1+M3 — explicit-skill / Action discovery path):
    parts.append(
        "  - For Action or explicit-skill requests, call list_skills first,"
    )
    parts.append(
        "    then invoke_skill (use describe_skill in between only when you need to inspect)."
    )
    parts.append(
        "  - If the user names a skill, use list_skills + invoke_skill"
    )
    parts.append("    rather than paraphrasing the request as a Reply.")
    # Bullet 3 (B3-H1+M3 — post-list_skills MUST):
    parts.append(
        "  - After list_skills reveals at least one matching skill, you MUST"
    )
    parts.append(
        "    call describe_skill or invoke_skill. Do NOT reply directly."
    )
    # Bullet 4 (B2-H1 — post-describe_skill MUST):
    parts.append(
        "  - After describe_skill, you MUST call invoke_skill or explain in text"
    )
    parts.append("    why not; never stop silently after investigation.")
    parts.append(
        "  - For Recall, answer from the Memory section's inlined descriptions;"
    )
    parts.append(
        "    use read_memory_body only when a description is too vague."
    )
    parts.append(
        "  - (list_memory is available for hierarchical browsing if needed.)"
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


_ENTRY_FULL_RE = re.compile(
    r"^\s*-\s*\[([^\]]+)\]\(([^)]+)\.md\)\s*[—–-]+\s*(.+)$"
)


def _parse_memory_entries(
    content: str,
) -> dict[str, list[tuple[str, str, str]]]:
    """Return {layer: [(slug, name, description), ...]} from merged memory
    index text. Layers are "shared" and "agent"."""
    shared: list[tuple[str, str, str]] = []
    agent: list[tuple[str, str, str]] = []
    current: list[tuple[str, str, str]] | None = None

    for line in content.splitlines():
        h = _SECTION_HEADER_RE.match(line.strip())
        if h:
            current = shared if h.group("layer") == "shared" else agent
            continue
        if current is None:
            continue
        m = _ENTRY_FULL_RE.match(line)
        if m:
            name, slug, desc = m.group(1), m.group(2), m.group(3).strip()
            current.append((slug, name, desc))
    return {"shared": shared, "agent": agent}


def _render_memory(memory_index: dict) -> list[str]:
    """Return lines for the memory section.

    Inline all entries with their descriptions so the LLM can answer
    recall queries directly without round-tripping through list_memory.
    Memory is bounded per agent (~tens of entries typically) so linear
    rendering is acceptable. read_memory_body remains available for cases
    where the description is too vague to answer from.
    """
    if memory_index.get("status") != "ok":
        return ["shared: (no entries)", "agent: (no entries)"]

    content = memory_index.get("content", "")
    entries = _parse_memory_entries(content)

    lines: list[str] = []
    for layer in ("shared", "agent"):
        layer_entries = entries.get(layer, [])
        if not layer_entries:
            lines.append(f"{layer}: (no entries)")
            continue
        lines.append(f"{layer}:")
        for slug, _name, desc in layer_entries:
            lines.append(f"  - {slug}: {desc}")
    return lines
