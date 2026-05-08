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

# ── G12 attractor mitigation (B7 finding: skill description verbosity trigger) ──
#
# Empty-stop attractor root cause: skill description verbosity.  B7 finding
# B7-G12-context-root-cause.md (commit a62a9dad) confirmed that truncating
# descriptions to ≤80 chars in list_skills tool_response reduced empty-stop
# rate from 100% → 0% (H-b verification).  B7-G12-cross-attractor-pattern.md
# (commit a947255e) confirmed two trigger paths:
#   Pattern A: via list_skills tool_response
#   Pattern C: via system prompt inline skill list
# Both paths must truncate to the same threshold.  describe_skill returns the
# full description (details on demand — list is summary only).
MAX_DESC_LEN_FOR_LISTING: int = 80

# ── G12 attractor mitigation — describe_skill routing field strip (B11-R2) ──
#
# describe_skill returns the full catalogue entry dict.  When that dict
# includes the ``routing`` block (intents / when_to_use / when_not_to_use /
# examples), the serialised tool_response can exceed 1000 chars and triggers
# the same P-b verbosity attractor that list_skills descriptions trigger
# (Pattern D — describe_skill response verbosity).
#
# B11-R2 N-shot experiment (synthetic trace, N=10):
#   - Full routing included (~1000 chars): 2/10 empty-stop (20%)
#   - Routing stripped (~187 chars):       0/10 empty-stop (0%)
#   - invoke_skill desc truncation alone:  1/10 — not significant
#
# The ``routing`` block is decision-guidance for BEFORE the router calls
# describe_skill.  Once the LLM has issued the describe_skill call it is
# committed to that skill; the routing guidance is no longer needed and only
# adds verbosity that triggers the P-b attractor.  ``category`` is internal
# grouping metadata also redundant for invocation.
#
# P7-clean: ``routing`` and ``category`` are OS-level catalogue metadata
# fields (not skill-specific names).  Filtering applied uniformly across all
# skills (no skill-name / phase-name / artifact-name literals hardcoded).
_DESCRIBE_SKILL_STRIP_FIELDS: frozenset[str] = frozenset({"routing", "category"})


# ── dispatch_kind sidecar registry ──────────────────────────────────────────
#
# Each tool is intrinsically either:
#   - "sync"  — invoker awaits a result that's available in this RouterLoop
#               turn; the LLM sees the tool_result and decides next step.
#   - "async" — invoker dispatches work whose result arrives via a separate
#               channel in a future router invocation (e.g. delegate_to_agent
#               result comes through PR14 pending_chain). The current loop
#               cannot wait for the answer; RouterLoop must exit after
#               dispatch and rely on the future invocation to resume.
#
# Default: any tool not listed here is treated as "sync".
#
# Future: when more async tools appear (long-running skill modes, scheduled
# tasks, webhooks), this registry can grow; the formalization candidate is
# the `ToolSpec` dataclass in the residuals (residuals → OS abstraction
# 拡張 → ToolSpec dataclass formalize).
_DISPATCH_KIND: dict[str, str] = {
    "delegate_to_agent": "async",
    # ADR-0023 Phase 2.1: plan-mode dispatch is fire-and-forget so the
    # chat turn doesn't block on multi-step LLM work. dispatch_plan_tool
    # spawns the PlanRuntime as a background task and returns the
    # spawn ack immediately; outbox narration carries progress + final
    # text. RouterLoop exits after dispatch (= same posture as
    # delegate_to_agent).
    "plan": "async",
}


def get_dispatch_kind(tool_name: str) -> str:
    """Return "sync" or "async" for the given tool name.

    Used by RouterLoop to decide whether to continue the loop after a
    tool dispatch (sync — result is in the tool_result, LLM can act on it)
    or to exit immediately and wait for a deferred result via a separate
    channel (async — pending_chain or equivalent).
    """
    return _DISPATCH_KIND.get(tool_name, "sync")


def build_tools(
    available_skills: list[dict],  # [{name, description, routing?}, ...]
    available_agents: list[dict],  # [{name, role}, ...]
    *,
    file_permissions: dict | None = None,  # {"read": [paths], "write": [paths]}
    mcp_servers: list[dict] | None = None,  # [{"name": ..., "description": ...}, ...]
    web_fetch_allowed: bool = False,        # operator opt-in (data-exfiltration risk)
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
        When the list is non-empty the ``name`` field of ``invoke_skill`` gets
        an ``enum`` constraint so dispatch_tool's schema validation rejects
        hallucinated skill names (S13b gap). When empty, plain ``string`` is
        used (no enum) to avoid an empty-enum schema that some providers reject.
    available_agents:
        Peer agent entries. Each dict must have at least ``name``.
        Same enum strategy as above for ``delegate_to_agent.to``.
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
    # RETRO-H1+H2 fix: dynamic enum injection for invoke_skill.name and
    # delegate_to_agent.to closes the schema-level hallucination gap (P4
    # alignment — LLM picks only from OS-provided candidates).
    #
    # History: PR37 wave 2D added enum; post-2D dogfood showed an attractor
    # side-effect ("hello" → ai_article_writer). That regression was caused by
    # surfacing skill names *only* in the schema without a flat list in the
    # system prompt — the LLM saw names but lacked context to judge relevance.
    # RETRO fix pairs enum (schema layer) with a flat list + one-line
    # description in the system prompt (context layer), giving the LLM both
    # constraint and context to resist the attractor.
    #
    # When available_skills is empty, invoke_skill is omitted from the tools
    # list to avoid an empty-enum schema that some providers reject.
    # Same strategy for available_agents / delegate_to_agent.
    skill_names = [s["name"] for s in available_skills]
    agent_names = [a["name"] for a in available_agents]
    _invoke_skill_name_schema: dict = (
        {"type": "string", "enum": skill_names}
        if skill_names
        else {"type": "string"}
    )
    _delegate_to_schema: dict = (
        {"type": "string", "enum": agent_names}
        if agent_names
        else {"type": "string"}
    )
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
                    "each with name and one-line description. "
                    "After this returns, narrate the skill names directly to "
                    "the user in your next message — do not stop after listing "
                    "and do not ask for confirmation before naming them."
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
        *(
            [
                {
                    "type": "function",
                    "function": {
                        "name": "invoke_skill",
                        "description": (
                            "Run a skill from the registered list. "
                            "The 'name' parameter MUST be one of the skills "
                            "listed in the system prompt's \"Available skills\" "
                            "section, used verbatim (no dots, no slashes, "
                            "no namespace prefixes). "
                            "Use list_skills' input_fields hint to construct "
                            "the correct input, or call describe_skill for full "
                            "schema details. Do not guess input field names."
                        ),
                        "parameters": {
                            "type": "object",
                            "properties": {
                                "name": {
                                    **_invoke_skill_name_schema,
                                    "description": (
                                        "Skill name — choose exactly one from "
                                        "the enum (verbatim, no dots or slashes)."
                                    ),
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
                }
            ]
            if skill_names
            else []
        ),
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
                            **_delegate_to_schema,
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
    #
    # File access tools are gated on the operator's `permissions.file.*`
    # declaration. The OS-level dispatch layer
    # (`permissions._in_default_read_zone`) does grant reads within the
    # project root by default, but exposing the tools without a matching
    # config declaration mixes "operator opt-in" with "OS auto-grant" in a
    # way that makes the safety boundary fuzzy — a previous attempt to
    # align the two layers (= unconditional tool exposure) was reverted
    # because it dragged the chat router into the user-file protection
    # surface. Reyn's own source / docs are accessed via the dedicated
    # `reyn_src_*` tools (see section F below), which carry no
    # permission-protected content and so don't need this gate.
    _file_read = (file_permissions or {}).get("read") or []
    _file_write = (file_permissions or {}).get("write") or []

    if _file_read or _file_write:
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
                        "Read a file's contents under the agent's read scope. "
                        "Common conventions: README is at project root as "
                        "`README.md`. CLAUDE.md, CHANGELOG.md, and "
                        "configuration files (e.g. `reyn.yaml`, "
                        "`pyproject.toml`) are at project root. Try these "
                        "conventional paths directly instead of asking the "
                        "user where the file lives."
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

    # ── E. Web tools (OS-native, backed by Control IR ops web/search +
    #         web/fetch). E1 web_search is always exposed (read-only, public
    #         queries — comparable security level to a logged query string).
    #         E2 web_fetch is opt-in: arbitrary URL fetches can be misused for
    #         data exfiltration (LLM bakes secrets into the URL and the
    #         attacker's server logs them) or to probe internal endpoints, so
    #         the operator enables it explicitly via `web.fetch: allow` in
    #         reyn.yaml.
    tools += [
        # ── E1: web_search (always available) ────────────────────────────────
        {
            "type": "function",
            "function": {
                "name": "web_search",
                "description": (
                    "Search the public web with DuckDuckGo and return "
                    "structured results. Standard search operators are "
                    "supported in `query`: `site:<domain>` to scope to "
                    "one site (e.g. `site:news.ycombinator.com`), "
                    "`\"phrase\"` for exact match, `-term` to exclude. "
                    "Use them when the user's intent is site-specific "
                    "or phrase-anchored; plain keywords work otherwise. "
                    "query: search string. "
                    "max_results: cap on returned results (default 5)."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string"},
                        "max_results": {"type": "integer"},
                    },
                    "required": ["query"],
                },
            },
        },
    ]

    # ── E2: web_fetch (operator opt-in via web.fetch: allow) ──────────────────
    if web_fetch_allowed:
        tools += [
            {
                "type": "function",
                "function": {
                    "name": "web_fetch",
                    "description": (
                        "Fetch a single URL and return its (text-extracted) "
                        "content. url: absolute http/https URL. "
                        "max_length: cap on returned content size "
                        "(default 50000). Use after web_search to read a "
                        "result page in detail."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "url": {"type": "string"},
                            "max_length": {"type": "integer"},
                        },
                        "required": ["url"],
                    },
                },
            },
        ]

    # ── G. Plan tool (always present) ────────────────────────────────────────
    #
    # `plan` lets the LLM decompose a complex query into 2-7 sub-tasks
    # that the OS executes in topological order, each in a narrow LLM
    # call (= focused tool catalog + step-specific system prompt). The
    # terminal step's reply becomes the user-facing answer.
    #
    # Why opt-in by the LLM (= just another tool, not a forced mode):
    # simple chat queries should still work via direct reply or single
    # tool call. Plan adds latency / cost (= 2-7 extra LLM calls per
    # turn), so the description nudges the LLM to use it ONLY when the
    # query genuinely needs multi-source synthesis.
    #
    # The schema enforces 2-7 steps, string ids, and strings-only tool
    # arrays. Per-step tool name validity (= must be in current
    # catalog) is checked at dispatch time by `parse_and_validate_plan`,
    # not here, because the tool list is dynamic per-session. Cycle
    # detection is also at dispatch time.
    tools += [
        # ── G1: plan ─────────────────────────────────────────────────────────
        {
            "type": "function",
            "function": {
                "name": "plan",
                "description": (
                    "Decompose a complex query into 2-7 independent "
                    "sub-tasks. Use ONLY when the query needs multi-"
                    "source synthesis (e.g. \"explain X with code "
                    "references\", \"compare A vs B from multiple "
                    "docs\", \"build a summary across these N "
                    "files\"). For simple queries — chitchat, single-"
                    "tool retrieval, single-source narration — reply "
                    "directly or call one tool; do NOT use plan. The "
                    "terminal step's text reply becomes the user-"
                    "facing answer; design the last step to "
                    "synthesise."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "goal": {
                            "type": "string",
                            "description": (
                                "1-sentence restatement of the user's overall query."
                            ),
                        },
                        # steps is a JSON-encoded string instead of a nested
                        # object array because the chat-router schema budget
                        # forbids depth-2 object properties (= Gemini-safe
                        # constraint, see test_nested_objects_max_depth_1).
                        # The dispatch layer parses + validates the JSON.
                        "steps_json": {
                            "type": "string",
                            "description": (
                                "JSON-encoded array of 2-7 step objects. Each "
                                "step has shape: "
                                "{\"id\": str, \"description\": str, "
                                "\"tools\": [str, ...], \"depends_on\": [str, ...]}. "
                                "id: short unique identifier. description: what "
                                "this step does. "
                                # 2026-05-07 dogfood fix: clarify step.tools field —
                                # LLM was confusing skill names (= invoke_skill enum
                                # values like \"direct_llm\") with top-level tool
                                # names. Be explicit about both the source of truth
                                # AND the empty-list semantics for synthesis steps.
                                "tools: list of TOP-LEVEL tool names this step "
                                "calls (e.g. \"reyn_src_read\", \"web_search\", "
                                "\"invoke_skill\"). Use [] for steps that just "
                                "synthesise / compare / summarise from prior step "
                                "outputs — the step's LLM does that natively without "
                                "any tool. To run a skill, use [\"invoke_skill\"], "
                                "NOT the skill's name. depends_on: ids of prior "
                                "steps whose output this step needs (default []). "
                                "The terminal step's text reply becomes the user-"
                                "facing answer; design the last step to "
                                "synthesise (= tools: []). Example: "
                                "[{\"id\": \"s1\", \"description\": \"read README\", "
                                "\"tools\": [\"reyn_src_read\"], \"depends_on\": []}, "
                                "{\"id\": \"s2\", \"description\": \"compare and "
                                "summarise for user\", "
                                "\"tools\": [], \"depends_on\": [\"s1\"]}]"
                            ),
                        },
                    },
                    "required": ["goal", "steps_json"],
                },
            },
        },
    ]

    # ── F. Reyn-source tools (always present, no permission) ────────────────
    #
    # `reyn_src_list` / `reyn_src_read` give the agent read access to
    # **Reyn's own** repository (= the project where pyproject.toml
    # declares Reyn). They serve a single use case: when the user asks
    # how Reyn works or wants a deep-dive into its implementation, the
    # agent should answer from Reyn's source/docs, not web search.
    #
    # Why no permission gate: the resolver scopes paths to the Reyn
    # repository tree, which is by definition public open-source content
    # (= GitHub secret-scanning blocks credentials at push time, so
    # nothing in the tree is sensitive). Operators don't configure this
    # — it's an OS-internal capability, distinct from `file_*` (= which
    # accesses the *user's* project files and IS permission-gated).
    #
    # Why two tools, not one: `list` lets the LLM discover the layout
    # before reading; `read` returns the file body. Mirrors the file_*
    # pair so the LLM's tool-use pattern is consistent across both
    # Reyn-source and user-file access.
    tools += [
        # ── F1: reyn_src_list ────────────────────────────────────────────────
        {
            "type": "function",
            "function": {
                "name": "reyn_src_list",
                "description": (
                    "List entries under a path inside Reyn's own repository "
                    "(= the project that built this agent). Pass \"\" for "
                    "the repo root. Returns names + types (file/dir). Use "
                    "this to discover Reyn's source/doc layout before "
                    "reading specific files. Examples: list \"\" for the "
                    "top-level layout, \"docs/en/concepts\" for concept "
                    "docs, \"src/reyn/chat\" for the chat layer source."
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
        # ── F2: reyn_src_read ────────────────────────────────────────────────
        {
            "type": "function",
            "function": {
                "name": "reyn_src_read",
                "description": (
                    "Read a text file from Reyn's own repository. Path is "
                    "repo-root-relative (= same paths the user sees on "
                    "GitHub). Start with reyn_src_read(\"README.md\") for "
                    "an overview and a curated index of deep-dive paths. "
                    "Use this for any \"how does Reyn / how does Reyn's X "
                    "work?\" question — Reyn's source is the authoritative "
                    "answer, not web search."
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
