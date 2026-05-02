"""Build the tools= argument for the native tool_use router loop (PR35).

Public API
----------
build_tools(available_skills, available_agents, *, file_permissions, mcp_servers)
    Returns 11–18 tools in fixed order for litellm.acompletion.

Gemini-safe schema rules enforced throughout:
- No oneOf / anyOf / additionalProperties / format keys
- Nested objects max 1 level (input: object / args: object are untyped)
- enum values are strings only
- Tool order is a literal list — deterministic regardless of dict iteration order
"""

from __future__ import annotations


def build_tools(
    available_skills: list[dict],  # [{name, description, routing?}, ...]
    available_agents: list[dict],  # [{name, role}, ...]
    *,
    file_permissions: dict | None = None,  # {"read": [paths], "write": [paths]}
    mcp_servers: list[dict] | None = None,  # [{"name": ..., "description": ...}, ...]
) -> list[dict]:
    """Build the tools= argument for litellm.acompletion.

    Returns 11–18 tools in fixed order (Anthropic prompt cache compatibility).
    Tool order matches the plan's canonical ordering:
      A1 list_skills, A2 describe_skill, A3 list_agents, A4 describe_agent,
      A5 list_memory, A6 read_memory_body,
      B1 invoke_skill, B2 delegate_to_agent,
      B3 remember_shared, B4 remember_agent, B5 forget_memory,
      C1 list_directory, C2 read_file (when any file scope),
      C3 write_file, C4 delete_file (only when write scope),
      D1 list_mcp_servers, D2 list_mcp_tools, D3 call_mcp_tool (when mcp configured).

    Parameters
    ----------
    available_skills:
        Skill catalogue entries. Each dict must have at least ``name``.
        Used only to document what names are valid; the parameter itself
        stays ``{"type": "string"}`` without enum (skill names can be many —
        discovery via list_skills / describe_skill).
    available_agents:
        Peer agent entries. Each dict must have at least ``name``.
        Same rationale — plain string, discovery via list_agents.
    file_permissions:
        Optional dict with ``read`` and/or ``write`` lists of path strings.
        - None or both empty → File tools omitted entirely (C1–C4).
        - read non-empty, write empty → include C1+C2 only.
        - write non-empty → include all 4 file tools (C1–C4).
    mcp_servers:
        Optional list of MCP server dicts (each with ``name`` and
        ``description``). None or [] → MCP tools omitted. Otherwise all 3
        MCP tools (D1–D3) are included.
    """
    # fmt: off
    tools: list[dict] = [
        # ── A1: list_skills ──────────────────────────────────────────────
        {
            "type": "function",
            "function": {
                "name": "list_skills",
                "description": (
                    "Browse the skill catalogue hierarchically. "
                    "Pass empty string to see top-level categories. "
                    "Pass a category path to drill in. "
                    "Returns either child categories or items, "
                    "each with name and one-line description."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {
                            "type": "string",
                            "description": (
                                'Category path, e.g. "", "write", "write/blog". '
                                "Empty = root."
                            ),
                        }
                    },
                    "required": ["path"],
                },
            },
        },
        # ── A2: describe_skill ───────────────────────────────────────────
        {
            "type": "function",
            "function": {
                "name": "describe_skill",
                "description": (
                    "Fetch full metadata for one skill: when_to_use, examples, "
                    "input artifact schema. "
                    "Call this before invoke_skill if you're unsure how to "
                    "construct the input."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string"},
                    },
                    "required": ["name"],
                },
            },
        },
        # ── A3: list_agents ──────────────────────────────────────────────
        {
            "type": "function",
            "function": {
                "name": "list_agents",
                "description": (
                    "Browse peer agents reachable via topology. "
                    "Pass empty path for clusters; "
                    "pass a cluster name for agents in it."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string"},
                    },
                    "required": ["path"],
                },
            },
        },
        # ── A4: describe_agent ───────────────────────────────────────────
        {
            "type": "function",
            "function": {
                "name": "describe_agent",
                "description": (
                    "Fetch full role / capabilities profile for one agent. "
                    "Call before delegate_to_agent if uncertain."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string"},
                    },
                    "required": ["name"],
                },
            },
        },
        # ── A5: list_memory ──────────────────────────────────────────────
        {
            "type": "function",
            "function": {
                "name": "list_memory",
                "description": (
                    'Browse persisted memory hierarchically. Path = "" (roots) '
                    '| "shared" | "shared/user" | "agent/feedback" etc. '
                    "Returns child categories or item entries "
                    "(slug + name + one-line description)."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string"},
                    },
                    "required": ["path"],
                },
            },
        },
        # ── A6: read_memory_body ─────────────────────────────────────────
        {
            "type": "function",
            "function": {
                "name": "read_memory_body",
                "description": (
                    "Fetch the full body of one memory entry. "
                    "Use only when list_memory's description is too vague "
                    "to answer the user."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "layer": {
                            "type": "string",
                            "enum": ["shared", "agent"],
                        },
                        "slug": {"type": "string"},
                    },
                    "required": ["layer", "slug"],
                },
            },
        },
        # ── B1: invoke_skill ─────────────────────────────────────────────
        {
            "type": "function",
            "function": {
                "name": "invoke_skill",
                "description": (
                    "Run a skill. "
                    "Construct input matching the skill's artifact schema "
                    "(call describe_skill first if unsure)."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "name": {
                            "type": "string",
                            "description": "Skill name as listed by list_skills.",
                        },
                        "input": {
                            "type": "object",
                            "description": (
                                "Skill input artifact: "
                                "{type: <artifact_type>, data: {...}}"
                            ),
                        },
                    },
                    "required": ["name", "input"],
                },
            },
        },
        # ── B2: delegate_to_agent ────────────────────────────────────────
        {
            "type": "function",
            "function": {
                "name": "delegate_to_agent",
                "description": "Forward the request to a peer agent.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "to": {
                            "type": "string",
                            "description": (
                                "Target agent name as listed by list_agents."
                            ),
                        },
                        "request": {
                            "type": "string",
                            "description": (
                                "Natural-language request paraphrased "
                                "for the peer's context."
                            ),
                        },
                    },
                    "required": ["to", "request"],
                },
            },
        },
        # ── B3: remember_shared ──────────────────────────────────────────
        {
            "type": "function",
            "function": {
                "name": "remember_shared",
                "description": (
                    "Persist a durable fact to project-wide (shared) memory. "
                    "Use for user role / project decisions / external references "
                    "that benefit all agents."
                ),
                "parameters": {
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
                },
            },
        },
        # ── B4: remember_agent ───────────────────────────────────────────
        {
            "type": "function",
            "function": {
                "name": "remember_agent",
                "description": (
                    "Persist a durable fact to this agent's private memory. "
                    "Use for agent-specific preferences, feedback, or context "
                    "that should not propagate to all agents."
                ),
                "parameters": {
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
                },
            },
        },
        # ── B5: forget_memory ────────────────────────────────────────────
        {
            "type": "function",
            "function": {
                "name": "forget_memory",
                "description": (
                    "Delete a memory entry. "
                    "Only when the user explicitly says 'forget' or "
                    "the memory turned out wrong."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "layer": {
                            "type": "string",
                            "enum": ["shared", "agent"],
                        },
                        "slug": {"type": "string"},
                    },
                    "required": ["layer", "slug"],
                },
            },
        },
    ]
    # fmt: on

    # ── C. File tools (permission-gated) ─────────────────────────────────────
    _file_read = (file_permissions or {}).get("read") or []
    _file_write = (file_permissions or {}).get("write") or []

    if _file_read or _file_write:
        # C1 and C2 are always present when any file scope is configured
        tools += [
            # ── C1: list_directory ───────────────────────────────────────────
            {
                "type": "function",
                "function": {
                    "name": "list_directory",
                    "description": (
                        "List contents of a directory under the agent's read scope. "
                        "Returns names + types (file/dir)."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "path": {"type": "string"},
                        },
                        "required": ["path"],
                    },
                },
            },
            # ── C2: read_file ────────────────────────────────────────────────
            {
                "type": "function",
                "function": {
                    "name": "read_file",
                    "description": (
                        "Read a file's contents under the agent's read scope."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "path": {"type": "string"},
                        },
                        "required": ["path"],
                    },
                },
            },
        ]

        if _file_write:
            # C3 and C4 only when write scope is configured
            tools += [
                # ── C3: write_file ───────────────────────────────────────────
                {
                    "type": "function",
                    "function": {
                        "name": "write_file",
                        "description": (
                            "Write content to a file under the agent's write scope. "
                            "Creates or overwrites."
                        ),
                        "parameters": {
                            "type": "object",
                            "properties": {
                                "path": {"type": "string"},
                                "content": {"type": "string"},
                            },
                            "required": ["path", "content"],
                        },
                    },
                },
                # ── C4: delete_file ──────────────────────────────────────────
                {
                    "type": "function",
                    "function": {
                        "name": "delete_file",
                        "description": (
                            "Delete a file under the agent's write scope."
                        ),
                        "parameters": {
                            "type": "object",
                            "properties": {
                                "path": {"type": "string"},
                            },
                            "required": ["path"],
                        },
                    },
                },
            ]

    # ── D. MCP tools (permission-gated) ──────────────────────────────────────
    if mcp_servers:
        tools += [
            # ── D1: list_mcp_servers ─────────────────────────────────────────
            {
                "type": "function",
                "function": {
                    "name": "list_mcp_servers",
                    "description": (
                        "List available MCP servers configured for this agent. "
                        "Returns name + description per server."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {},
                        "required": [],
                    },
                },
            },
            # ── D2: list_mcp_tools ───────────────────────────────────────────
            {
                "type": "function",
                "function": {
                    "name": "list_mcp_tools",
                    "description": (
                        "List tools exposed by one MCP server "
                        "(with description per tool)."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "server": {"type": "string"},
                        },
                        "required": ["server"],
                    },
                },
            },
            # ── D3: call_mcp_tool ────────────────────────────────────────────
            {
                "type": "function",
                "function": {
                    "name": "call_mcp_tool",
                    "description": (
                        "Invoke an MCP server tool. Construct args matching "
                        "the tool's input schema (see list_mcp_tools)."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "server": {"type": "string"},
                            "tool": {"type": "string"},
                            "args": {"type": "object"},
                        },
                        "required": ["server", "tool", "args"],
                    },
                },
            },
        ]

    return tools
