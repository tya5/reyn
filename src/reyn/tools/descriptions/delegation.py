"""Tool descriptions for the ``delegation`` category.

Phase 2 of the tool-description package refactor (byte-identical
relocation — no LLM-facing text change): every ``delegation``-category
ToolDefinition's description string lives here as a reviewable
``ToolDescription`` record. Each ``.text`` value is copied verbatim from
its origin tool module; the origin module now aliases its
``_X_DESCRIPTION`` module constant to ``delegation.NAME.text`` so every
call site is unchanged.

Covers: spawn_agent (#2103 B-tool, renamed from agent_spawn — #4004),
spawn_session (#2103 S1bc, renamed from session_spawn — #4004),
create_topology (#2103 C1, renamed from topology_create — #4004),
send_to_session (proposal 0067 P5, #3978 — new, not a relocation: authored
directly here), run_prompt (proposal 0067 P4d, #3978). All are router-only
— org-design / peer-messaging primitives the LLM drives directly.

delegate_to_agent (formerly here, ADR-0026 M4) retired in proposal 0067 P6
(#3978) — send_to_session (fire-and-forget) and run_prompt (synchronous,
collect="attached") together cover what it did, minus one gap: no
fire-and-forget-with-a-later-collected-reply verb exists yet (that is
run_prompt's own eventual collect="async", deliberately sequenced AFTER P6
per the proposal's own P4/P6 ordering note, to avoid building a second
async-collection path P6 would immediately have to fold into the same one
delegate_to_agent used).
"""
from __future__ import annotations

from reyn.tools.descriptions._types import ParamDescription, ToolDescription

agent_spawn = ToolDescription(
    tool_name="spawn_agent",
    surfaced="router (gates.router=allow) — #2103 B-tool",
    purpose=(
        "Create a new agent (org-design: WHO) under the caller's own "
        "authority, with capability automatically capped at a subset of "
        "the spawner's (⊆-parent by construction)."
    ),
    text=(
        "Create a new agent under your authority (org-design): give it a name + role. The "
        "new agent's capabilities are automatically capped at a SUBSET of your own (it can "
        "never do anything you can't). Use to design a team/org of agents; to narrow a "
        "member's capabilities further or wire who-can-message-whom, use create_topology."
    ),
    ja=(
        "自分の権限の下で新しいエージェントを作成する（組織設計: WHO）。"
        "名前とロールを与える。新エージェントの権限は自動的に自分のサブ"
        "セットに制限される（自分にできないことはできない）。エージェント"
        "チーム/組織を設計する用途。メンバーの権限をさらに絞ったり、誰が"
        "誰にメッセージできるかを配線するには create_topology を使う。"
    ),
)

session_spawn = ToolDescription(
    tool_name="spawn_session",
    surfaced="router (gates.router=allow) — #2103 S1bc",
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
    tool_name="create_topology",
    surfaced="router (gates.router=allow) — #2103 C1",
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
        "spawn subtree (yourself or agents you created via spawn_agent) — a member's "
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

send_to_session = ToolDescription(
    tool_name="send_to_session",
    surfaced="router (gates.router=allow) — proposal 0067 P5 (#3978)",
    purpose=(
        "Deliver a message to a specific (agent, session) pair — "
        "fire-and-forget, no reply is collected. Pairs with run_prompt, "
        "which waits for a reply."
    ),
    text=(
        "Send a message to a specific session of an agent (delivery only — no reply is "
        "collected; use run_prompt if you need one, on a session that's currently idle). "
        "Set wake=True to have the target start a turn on it now; wake=False (default) "
        "queues it as context for whenever the target next runs a turn on its own."
    ),
    ja=(
        "指定した (agent, session) にメッセージを配送する（配送のみ——応答は"
        "収集しない。応答が必要で対象セッションが待機中なら run_prompt を使う）。"
        "wake=True で相手に今すぐターンを開始させる。wake=False（既定）は相手が"
        "次に自分でターンを実行するまでのコンテキストとしてキューに入れる。"
    ),
)

run_prompt = ToolDescription(
    tool_name="run_prompt",
    surfaced="router (gates.router=allow) — proposal 0067 P4d (#3978)",
    purpose=(
        "Send a prompt to a LIVE peer (agent, session) and collect its "
        "reply in-band, synchronously. Pairs with send_to_session (delivery "
        "only, no reply)."
    ),
    text=(
        "Run a prompt on a specific session of an agent and wait for its reply "
        "in-band (collect=\"attached\" — currently the only supported value; an "
        "async variant that returns a handle immediately is not yet available). "
        "The target must already be a LIVE session that is not currently "
        "running its own turn — this does not spawn a session, and refuses "
        "with a named error if the target is busy. Use send_to_session instead "
        "if you don't need to wait for a reply."
    ),
    ja=(
        "指定した (agent, session) にプロンプトを送り、応答を待って同じ場で"
        "受け取る（collect=\"attached\" — 現時点で対応する唯一の値。即座に"
        "handle を返す非同期版はまだ利用できない）。対象は既に生きている"
        "session でなければならず、かつ自分自身のターンを実行中でないこと"
        "（このツールはセッションを生成しない。対象が busy なら、理由を"
        "名指しした error を返す）。応答を待つ必要がなければ send_to_session"
        "を使うこと。"
    ),
)

ALL: dict[str, ToolDescription] = {
    "spawn_agent": agent_spawn,
    "spawn_session": session_spawn,
    "create_topology": topology_create,
    "send_to_session": send_to_session,
    "run_prompt": run_prompt,
}


# ── Phase 4: per-parameter descriptions (byte-identical relocation) ──────────

PARAMS: dict[str, dict[str, ParamDescription]] = {
    "spawn_agent": {
        "name": ParamDescription(
            text="The new agent's identity (a unique agent name).",
            ja="新しいエージェントの識別子（一意なエージェント名）。",
        ),
        "role": ParamDescription(
            text="The new agent's role/purpose (free text).",
            ja="新しいエージェントの役割・目的（自由記述）。",
        ),
    },
    "spawn_session": {
        "request": ParamDescription(
            text="The task for the fresh-context session to run.",
            ja="新規コンテキストのセッションに実行させるタスク。",
        ),
        "mode": ParamDescription(
            text=(
                "ephemeral = the session auto-vanishes after its task; "
                "persistent = it stays. Chosen at spawn time."
            ),
            ja=(
                "ephemeral はタスク後にセッションが自動消滅、persistent は"
                "残る。スポーン時に選択する。"
            ),
        ),
        "narrowing": ParamDescription(
            text=(
                "Optional per-session capability narrowing (restrict-only, cannot "
                "widen your envelope — your OWN per-session narrowing is composed "
                "in whatever you write here, denies union and allow-lists "
                "intersect): a capability_profile subset, e.g. "
                "{\"tool_deny\": [\"exec\"]}."
            ),
            ja=(
                "任意のセッション単位の権限絞り込み（restrict-only、自分の"
                "権限範囲を広げることはできない——ここに何を書いても自分自身の"
                "セッション単位の絞り込みが合成される。deny は和、allow は積）: "
                "capability_profile の"
                "サブセット、例 {\"tool_deny\": [\"exec\"]}。"
            ),
        ),
    },
    "create_topology": {
        "name": ParamDescription(
            text="The new topology's name (unique; 1-32 chars [a-z0-9_-]).",
            ja="新しいトポロジー名（一意、1〜32文字 [a-z0-9_-]）。",
        ),
        "kind": ParamDescription(
            text=(
                "network = every member ↔ every member; team = star around a leader "
                "(requires 'leader'); pipeline = ordered chain (members[i] → members[i+1])."
            ),
            ja=(
                "network=全メンバー相互接続、team=leader中心のスター"
                "（'leader'必須）、pipeline=順序付きチェーン"
                "（members[i] → members[i+1]）。"
            ),
        ),
        "members": ParamDescription(
            text=(
                "Agent names to wire — each must be in your spawn subtree (yourself or "
                "an agent you spawned)."
            ),
            ja=(
                "接続するエージェント名 — それぞれ自分のスポーンサブツリー内"
                "（自分自身か自分がスポーンしたエージェント）である必要がある。"
            ),
        ),
        "leader": ParamDescription(
            text="For kind=team only: the member at the centre of the star.",
            ja="kind=team のときのみ: スターの中心となるメンバー。",
        ),
        "profiles": ParamDescription(
            text=(
                "Optional JSON object mapping a member name to a capability_profile name "
                "(both strings). A bound member's session is narrowed by that profile (it "
                "can only narrow within its ⊆-you envelope, never widen). Each key must be "
                "one of 'members'."
            ),
            ja=(
                "任意: メンバー名を capability_profile 名にマッピングする JSON "
                "オブジェクト（両方とも文字列）。束縛されたメンバーのセッションは"
                "そのプロファイルで絞り込まれる（⊆自分の範囲内でのみ絞り込め、"
                "広げることはできない）。各キーは 'members' のいずれかである"
                "必要がある。"
            ),
        ),
    },
    "send_to_session": {
        "agent": ParamDescription(
            text="Target agent name as listed by list_agents.",
            ja="list_agents に列挙される送信先エージェント名。",
        ),
        "session": ParamDescription(
            text="Target session id (e.g. 'main', or a sid from list_tasks/describe_task).",
            ja="送信先のセッション id（例: 'main'、または list_tasks/describe_task の sid）。",
        ),
        "text": ParamDescription(
            text="Message body to deliver.",
            ja="配送するメッセージ本文。",
        ),
        "wake": ParamDescription(
            text=(
                "True = the target starts a turn on this message now. "
                "False (default) = queue it as context for the target's next turn."
            ),
            ja=(
                "True = 相手がこのメッセージで即座にターンを開始する。"
                "False（既定）= 相手の次のターンのコンテキストとしてキューに入れる。"
            ),
        ),
    },
    "run_prompt": {
        "agent": ParamDescription(
            text="Target agent name as listed by list_agents.",
            ja="list_agents に列挙される送信先エージェント名。",
        ),
        "session": ParamDescription(
            text=(
                "Target session id (e.g. 'main', or a sid from "
                "list_tasks/describe_task). Must already be a LIVE session — "
                "this does not spawn one."
            ),
            ja=(
                "送信先のセッション id（例: 'main'、または "
                "list_tasks/describe_task の sid）。既に生きている session で"
                "なければならない——このツールはセッションを生成しない。"
            ),
        ),
        "prompt": ParamDescription(
            text="The prompt to run on the target session.",
            ja="対象セッションで実行するプロンプト。",
        ),
        "collect": ParamDescription(
            text=(
                "How to receive the result. \"attached\" (the only supported "
                "value today) waits inline and returns the reply text."
            ),
            ja=(
                "結果の受け取り方。\"attached\"（現時点で対応する唯一の値）は"
                "その場で待ち、応答テキストを返す。"
            ),
        ),
    },
}
