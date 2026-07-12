"""Tool descriptions for the ``catalog`` bucket.

Phase 3 of the tool-description package refactor (byte-identical
relocation — no LLM-facing text change): the peer-agent catalog browse
tools (``list_agents`` / ``describe_agent``, ADR-0026 M3 Wave 2, from
``tools/catalog.py``) plus ``invoke_action`` (the universal catalog's
single dispatch entry point, from ``tools/universal_catalog.py`` — its
sibling discovery entries ``list_actions`` / ``search_actions`` /
``describe_action`` were already relocated into ``descriptions.discovery``
in Phase 1). Each ``.text`` value is copied verbatim from its origin tool
module; the origin module now aliases its ``_X_DESCRIPTION`` constant to
``catalog.NAME.text``.

Note: ``list_agents`` / ``describe_agent`` carry ``ToolDefinition.category
="discovery"`` and ``invoke_action`` carries ``category="invocation"`` — this
module groups them by feature-area (catalog browse + dispatch), matching
the ``mcp`` / ``io`` precedent set in Phase 2 (module grouping is
conceptual, not a literal mirror of the ``category`` field).
"""
from __future__ import annotations

from reyn.tools.descriptions._types import ToolDescription

list_agents = ToolDescription(
    tool_name="list_agents",
    surfaced="router + phase (gates.router=allow, gates.phase=allow)",
    purpose=(
        "Browse peer agents reachable via topology (clusters, then agents "
        "in a cluster) before delegating."
    ),
    text=(
        "Browse peer agents reachable via topology. "
        "Pass empty path for clusters; "
        "pass a cluster name for agents in it."
    ),
    ja=(
        "トポロジー経由で到達可能なピアエージェントを閲覧する。空の path "
        "でクラスタ一覧、クラスタ名を渡すとそのクラスタ内のエージェント"
        "一覧を返す。"
    ),
)

describe_agent = ToolDescription(
    tool_name="describe_agent",
    surfaced="router + phase (gates.router=allow, gates.phase=allow)",
    purpose=(
        "Fetch one peer agent's full role/capabilities profile before "
        "delegating to it, when uncertain it fits the task."
    ),
    text=(
        "Fetch full role / capabilities profile for one agent. "
        "Call before delegate_to_agent if uncertain."
    ),
    ja=(
        "1つのエージェントの完全な役割・能力プロファイルを取得する。"
        "delegate_to_agent の前に、そのエージェントが適任か不確かな場合"
        "に呼ぶ。"
    ),
)

invoke_action = ToolDescription(
    tool_name="invoke_action",
    surfaced=(
        "router-only (gates.router=allow, gates.phase=deny) — the universal "
        "catalog's single dispatch entry point for all 13 action categories"
    ),
    purpose=(
        "Execute any catalog action by qualified name — the ONE dispatch "
        "surface every action category (MCP tool, file op, web search, "
        "memory write, semantic search, agent delegation, etc.) routes "
        "through, so the LLM never needs a per-kind legacy tool."
    ),
    text=(
        "WHAT: Execute an action by qualified name (<category>__<entry>). "
        "Executes the action's default semantic operation. "
        "WHEN: Call this whenever you intend to run any action — MCP tool, "
        "file operation, web search, memory write, semantic search, etc. All catalog actions "
        "are invoked through this single entry point. "
        "WHEN NOT: For chitchat or self-questions, reply without tools. "
        "PREFERRED OVER: Legacy per-kind tools (call_mcp_tool, etc.) — "
        "invoke_action covers all 13 action categories uniformly. "
        "On unknown action_name, returns an error with similar-name suggestions. "
        ""
        "SPAWN-ACK HANDLING: when an action result is {status:'spawned', ...}, the "
        "router exits the current turn before this tool description applies; the OS "
        "emits the user-visible acknowledgment directly. You will not be asked to "
        "compose a reply for the spawn-ack turn. "
        ""
        "TASK_SPAWNED: an agent-role message starting with [task_spawned] is "
        "OS-emitted when an async task is launched (kind=agent, paired "
        "with chain_id). The structured header "
        "lets you correlate the spawn with the later [task_completed] message "
        "carrying the same identifier. The trailing human-readable line is "
        "what the user sees; the header is your correlation record. "
        ""
        "TASK_COMPLETED: a user-role message starting with [task_completed] is "
        "OS-injected when a previously-spawned async task finishes (kind=agent) "
        "or a spawned session completes (kind=spawned_session). The message "
        "carries the task's status + result "
        "fields. status='finished' means normal completion; other values "
        "('loop_limit_exceeded', 'phase_budget_exceeded', 'budget_exceeded', "
        "'error', or any non-'finished' value with result.error present) "
        "indicate the task did not complete normally. "
        ""
        "AGENT DELEGATION: For peer agent delegation, use "
        "action_name='multi_agent__delegate' with args {to: '<agent_name>', "
        "request: ...}; get its canonical args via "
        "describe_action(action_name='multi_agent__delegate'). "
        "Use when task is outside available actions but matches a peer agent's role, "
        "or when user explicitly addresses a named agent. "
        "Acknowledge delegation in 1 sentence."
    ),
    ja=(
        "修飾名（<category>__<entry>）でアクションを実行する。MCP ツー"
        "ル・ファイル操作・ウェブ検索・メモリ書き込み・意味検索など、"
        "あらゆるカテゴリのアクションはこの単一のディスパッチ入口を通"
        "して実行される。未知の action_name にはエラーと類似名の提案が"
        "返る。ピアエージェントへの委任は "
        "action_name='multi_agent__delegate' を使う。"
    ),
)

ALL: dict[str, ToolDescription] = {
    "list_agents": list_agents,
    "describe_agent": describe_agent,
    "invoke_action": invoke_action,
}
