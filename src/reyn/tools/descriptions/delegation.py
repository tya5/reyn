"""Tool descriptions for the ``delegation`` category.

Phase 2 of the tool-description package refactor (byte-identical
relocation — no LLM-facing text change): every ``delegation``-category
ToolDefinition's description string lives here as a reviewable
``ToolDescription`` record. Each ``.text`` value is copied verbatim from
its origin tool module; the origin module now aliases its
``_X_DESCRIPTION`` module constant to ``delegation.NAME.text`` so every
call site is unchanged.

Covers: agent_spawn (#2103 B-tool), delegate_to_agent (ADR-0026 M4),
session_spawn (#2103 S1bc), topology_create (#2103 C1). All four are
router-only (gates.phase=deny) — org-design / delegation primitives the
LLM drives directly, never a phase-authored Control IR op.
"""
from __future__ import annotations

from reyn.tools.descriptions._types import ToolDescription

agent_spawn = ToolDescription(
    tool_name="agent_spawn",
    surfaced="router-only (gates.router=allow, gates.phase=deny) — #2103 B-tool",
    purpose=(
        "Create a new agent (org-design: WHO) under the caller's own "
        "authority, with capability automatically capped at a subset of "
        "the spawner's (⊆-parent by construction)."
    ),
    text=(
        "Create a new agent under your authority (org-design): give it a name + role. The "
        "new agent's capabilities are automatically capped at a SUBSET of your own (it can "
        "never do anything you can't). Use to design a team/org of agents; to narrow a "
        "member's capabilities further or wire who-can-message-whom, use topology_create."
    ),
    ja=(
        "自分の権限の下で新しいエージェントを作成する（組織設計: WHO）。"
        "名前とロールを与える。新エージェントの権限は自動的に自分のサブ"
        "セットに制限される（自分にできないことはできない）。エージェント"
        "チーム/組織を設計する用途。メンバーの権限をさらに絞ったり、誰が"
        "誰にメッセージできるかを配線するには topology_create を使う。"
    ),
)

delegate_to_agent = ToolDescription(
    tool_name="delegate_to_agent",
    surfaced=(
        "router-only (gates.router=allow, gates.phase=deny) — async-dispatch "
        "(ADR-0026 §6): reply arrives in a future RouterLoop turn via "
        "PR14 pending_chain"
    ),
    purpose=(
        "Forward the current request to a peer agent for it to handle, "
        "without waiting inline for the reply."
    ),
    text="Forward the request to a peer agent.",
    ja=(
        "現在のリクエストをピアエージェントに転送する。応答はこの場では"
        "待たず、将来の RouterLoop ターンで届く（非同期ディスパッチ）。"
    ),
)

session_spawn = ToolDescription(
    tool_name="session_spawn",
    surfaced="router-only (gates.router=allow, gates.phase=deny) — #2103 S1bc",
    purpose=(
        "Spawn a fresh-context session under the caller's agent to run a "
        "task in isolation (ephemeral or persistent), optionally with "
        "narrowed capabilities."
    ),
    text=(
        "Spawn a fresh-context session under your agent to run a task in isolation. "
        "Choose mode='ephemeral' (auto-vanishes after the task) or 'persistent'. "
        "Optionally narrow the sub-session's capabilities (restrict-only). The session "
        "runs the task; its result stays in that session."
    ),
    ja=(
        "自分のエージェントの下に、タスクを隔離環境で実行するための新規"
        "コンテキストセッションを生成する。mode='ephemeral'（タスク後に"
        "自動消滅）または 'persistent' を選ぶ。サブセッションの権限を"
        "（制限のみ）狭めることもできる。セッションはタスクを実行し、結果"
        "はそのセッション内に留まる。"
    ),
)

topology_create = ToolDescription(
    tool_name="topology_create",
    surfaced="router-only (gates.router=allow, gates.phase=deny) — #2103 C1",
    purpose=(
        "Wire the caller's spawned agents into a topology (org-design: "
        "WIRING) controlling who-can-message-whom, and optionally bind "
        "members to a capability_profile to narrow them further."
    ),
    text=(
        "Wire agents you spawned into a topology (org-design): group them by kind "
        "(network = all-to-all, team = star around a leader, pipeline = ordered chain) to "
        "control who-can-message-whom, and optionally bind each member to a "
        "capability_profile to narrow it further. You may only include agents in your own "
        "spawn subtree (yourself or agents you created via agent_spawn) — a member's "
        "capabilities stay capped at a SUBSET of yours."
    ),
    ja=(
        "自分が生成したエージェントをトポロジーに配線する（組織設計: "
        "WIRING）。kind（network=全対全、team=リーダー中心のスター、"
        "pipeline=順序付きチェーン）でグループ化し、誰が誰にメッセージ"
        "できるかを制御する。任意でメンバーを capability_profile に束縛"
        "してさらに絞ることもできる。含められるのは自分のスポーン"
        "サブツリー内のエージェントのみで、権限は常に自分のサブセットに"
        "制限される。"
    ),
)

ALL: dict[str, ToolDescription] = {
    "agent_spawn": agent_spawn,
    "delegate_to_agent": delegate_to_agent,
    "session_spawn": session_spawn,
    "topology_create": topology_create,
}
