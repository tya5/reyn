"""Tool descriptions for the ``memory`` category.

Phase 2 of the tool-description package refactor (byte-identical
relocation — no LLM-facing text change): every ``memory``-category
ToolDefinition's description string lives here as a reviewable
``ToolDescription`` record. Each ``.text`` value is copied verbatim from
its origin tool module (``memory.py``); the origin module now aliases
its ``_X_DESCRIPTION`` module constant to ``memory.NAME.text`` so every
call site is unchanged.

Covers: list_memory, read_memory_body, remember_shared, remember_agent,
forget_memory.
"""
from __future__ import annotations

from reyn.tools.descriptions._types import ToolDescription

list_memory = ToolDescription(
    tool_name="list_memory",
    surfaced=(
        "router + phase (gates.router=allow, gates.phase=allow) — Type C "
        "closure per ADR-0026"
    ),
    purpose=(
        "Browse persisted memory hierarchically (shared/agent layers) to "
        "find a relevant entry before deciding whether read_memory_body is "
        "needed."
    ),
    text=(
        'Browse persisted memory hierarchically. Path = "" (roots) '
        '| "shared" | "shared/user" | "agent/feedback" etc. '
        "Returns child categories or item entries "
        "(slug + name + one-line description)."
    ),
    ja=(
        "永続化されたメモリを階層的に閲覧する。path は \"\"（ルート）/ "
        "\"shared\" / \"shared/user\" / \"agent/feedback\" 等。子カテゴリ"
        "または項目（slug + name + 一行説明）を返す。"
    ),
)

read_memory_body = ToolDescription(
    tool_name="read_memory_body",
    surfaced=(
        "router + phase (gates.router=allow, gates.phase=allow) — Type C "
        "closure per ADR-0026"
    ),
    purpose=(
        "Fetch the full body of one memory entry when list_memory's "
        "one-line description isn't enough to answer the user."
    ),
    text=(
        "Fetch the full body of one memory entry. "
        "Use only when list_memory's description is too vague "
        "to answer the user."
    ),
    ja=(
        "1つのメモリエントリの本文全体を取得する。list_memory の説明では"
        "ユーザーへの回答に不十分な場合にのみ使う。"
    ),
)

remember_shared = ToolDescription(
    tool_name="remember_shared",
    surfaced=(
        "router + phase (gates.router=allow, gates.phase=allow) — Type C "
        "closure per ADR-0026"
    ),
    purpose=(
        "Persist a durable, project-wide fact (user role / project decision "
        "/ external reference) that should benefit every agent."
    ),
    text=(
        "Persist a durable fact to project-wide (shared) memory. "
        "Use for user role / project decisions / external references "
        "that benefit all agents."
    ),
    ja=(
        "プロジェクト全体（共有）メモリに永続的な事実を記録する。ユーザー"
        "の役割・プロジェクトの決定事項・外部参照など、全エージェントに"
        "有益な情報に使う。"
    ),
)

remember_agent = ToolDescription(
    tool_name="remember_agent",
    surfaced=(
        "router + phase (gates.router=allow, gates.phase=allow) — Type C "
        "closure per ADR-0026"
    ),
    purpose=(
        "Persist a durable fact to this agent's own private memory (a "
        "preference/feedback/context that should not propagate to other "
        "agents)."
    ),
    text=(
        "Persist a durable fact to this agent's private memory. "
        "Use for agent-specific preferences, feedback, or context "
        "that should not propagate to all agents."
    ),
    ja=(
        "このエージェント自身の個人メモリに永続的な事実を記録する。他の"
        "エージェントに広めるべきでない、エージェント固有の好み・"
        "フィードバック・文脈に使う。"
    ),
)

forget_memory = ToolDescription(
    tool_name="forget_memory",
    surfaced=(
        "router + phase (gates.router=allow, gates.phase=allow) — Type C "
        "closure per ADR-0026"
    ),
    purpose=(
        "Delete a memory entry, restricted to explicit user intent (the "
        "user says 'forget') or a discovered-wrong memory."
    ),
    text=(
        "Delete a memory entry. Only when the user explicitly says "
        "'forget' or the memory turned out wrong."
    ),
    ja=(
        "メモリエントリを削除する。ユーザーが明示的に「忘れて」と言った"
        "場合、またはそのメモリが誤りだったと判明した場合にのみ使う。"
    ),
)

ALL: dict[str, ToolDescription] = {
    "list_memory": list_memory,
    "read_memory_body": read_memory_body,
    "remember_shared": remember_shared,
    "remember_agent": remember_agent,
    "forget_memory": forget_memory,
}
