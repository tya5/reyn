---
type: agent
topic: architecture
audience: [agent, human]
---

# Glossary — canonical reyn terms

Authoritative names for reyn concepts. Use these terms verbatim in agent
configuration and documentation. Translations are for prose only — code,
frontmatter keys, and CLI flags stay in English.

## Core layers

| English | 日本語 | Definition |
|---------|--------|------------|
| Agent | エージェント | A long-lived Session with its own profile, history, memory layer, and inbox. The interpreter of user intent. Persisted under `.reyn/agents/<name>/`. |
| Agent Profile | エージェントプロファイル | `.reyn/agents/<name>/profile.yaml` declaring `name`, `role`, `created_at`. |
| Agent Registry | エージェントレジストリ | Process-scoped owner of all loaded Session instances. Routes attach/detach and inter-agent messaging. |
| OS | OS | The runtime executor; sole owner of control flow. |
| Workspace | ワークスペース | The shared store for files and artifacts. |
| Event | イベント | A recorded state change. |

## Multi-agent

| English | 日本語 | Definition |
|---------|--------|------------|
| Topology | トポロジー | A declared communication structure (`network` / `team` / `pipeline`) listing members and edge rules. Persisted at `.reyn/topologies/<name>.yaml`. |
| `_default` topology | デフォルトトポロジー | Auto-managed network containing every agent that does NOT belong to any user-declared topology. In-memory, recomputed on demand. |
| Chain | チェイン | One logical request path from a top-level user submission, possibly spanning multiple agents and hops. Identified by `chain_id`. |
| `chain_id` | チェイン ID | uuid4 hex minted by `submit_user_text`; propagated through every inbox payload, history meta, and event in the same chain. |
| Pending Chain | ペンディングチェイン | State held in a delegating agent while it waits for delegate responses (deferred reply). Cleared when `waiting_on` becomes empty. |
| Hop Depth | ホップ深度 | Number of agent-to-agent forwards from the original user request. Bounded by `safety.loop.max_agent_hops`. |

## Permission verbs

| Verb | Meaning |
|------|---------|
| `allow` | Always permit, no prompt. |
| `ask` | Prompt the user the first time; persist the choice. |
| `deny` | Reject without prompt. |

## TUI vocabulary

Terms you will encounter in the conv pane, events tab, agents tab, or memory tab of the reyn TUI. They describe runtime UX surfaces.

| English | 日本語 | Definition |
|---------|--------|------------|
| Attached agent / attach pointer | アタッチ済みエージェント | The agent your TUI session currently talks to — the one labelled "you" in `/agents` output. Switch with `/attach <name>`. The attached agent receives your text submissions and dispatches them; non-attached agents continue running in the background. See `docs/concepts/multi-agent/multi-agent.md`. |
| ARS (Action Retrieval Service) / Hot list | ARS / ホットリスト | The action-routing service that ranks action candidates by recent use ("hot now"). The "HOT NOW" section of the Memory tab shows the current top of this list. See `docs/concepts/tools-integrations/universal-catalog.md`. |
| Checkpoint (snapshot + WAL) | チェックポイント | A durable point in a run where state is persisted (snapshot) and subsequent transitions append to a write-ahead log (WAL). `safety_limit_checkpoint` events fire when a checkpoint is taken. Enables crash recovery. See `docs/concepts/runtime/time-travel.md`. |
| Compaction | コンパクション | Automatic summarisation of older conv-pane history when the context window approaches its limit. Surfaced in the TUI as a `── ↑ compaction summary saved ────` divider in the conv pane and an "earlier history trimmed (N lines)" sticky warning. See `docs/concepts/data-retrieval/chat-compaction.md`. |
| Intervention | インターベンション | A question or confirmation an agent asks back, surfaced in the TUI as an orange-bordered chip widget. Answered inline by submitting text (head intervention) or via `/answer <id-prefix> <text>` (non-head, from the queue). See `docs/concepts/multi-agent/multi-agent.md`. |
