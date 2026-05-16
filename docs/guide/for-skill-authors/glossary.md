---
type: agent
topic: architecture
audience: [agent, human]
---

# Glossary — canonical reyn terms

Authoritative names for reyn concepts. Use these terms verbatim in skill DSL files, phase instructions, and documentation. Translations are for prose only — code, frontmatter keys, and CLI flags stay in English.

## Core layers

| English | 日本語 | Definition |
|---------|--------|------------|
| Agent | エージェント | A long-lived ChatSession with its own profile, history, memory layer, and inbox. The interpreter of user intent. Persisted under `.reyn/agents/<name>/`. |
| Agent Profile | エージェントプロファイル | `.reyn/agents/<name>/profile.yaml` declaring `name`, `role`, `created_at`, optional `allowed_skills`. |
| Agent Registry | エージェントレジストリ | Process-scoped owner of all loaded ChatSession instances. Routes attach/detach and inter-agent messaging. |
| Skill | スキル | A directory defining a phase graph and final output schema. |
| Phase | フェーズ | A reusable processing unit declaring only its `input` and instructions. |
| OS | OS | The runtime executor; sole owner of control flow. |
| Workspace | ワークスペース | The shared store for files and artifacts. |
| Artifact | アーティファクト | Structured data passed between phases. |
| Event | イベント | A recorded state change. |

## Multi-agent

| English | 日本語 | Definition |
|---------|--------|------------|
| Topology | トポロジー | A declared communication structure (`network` / `team` / `pipeline`) listing members and edge rules. Persisted at `.reyn/topologies/<name>.yaml`. |
| `_default` topology | デフォルトトポロジー | Auto-managed network containing every agent that does NOT belong to any user-declared topology. In-memory, recomputed on demand. |
| Chain | チェイン | One logical request path from a top-level user submission, possibly spanning multiple agents and hops. Identified by `chain_id`. |
| `chain_id` | チェイン ID | uuid4 hex minted by `submit_user_text`; propagated through every inbox payload, history meta, and event in the same chain. |
| Pending Chain | ペンディングチェイン | State held in a delegating agent while it waits for delegate responses (deferred reply). Cleared when `waiting_on` becomes empty. |
| `allowed_skills` | 許可スキル一覧 | Optional `list[str] \| None` in profile.yaml. `None` = unrestricted, `[]` = router-only, `[a, b]` = allowlist. stdlib router/compactor are not subject. (FP-0011 removed `skill_narrator`; the router LLM narrates inline.) |
| Hop Depth | ホップ深度 | Number of agent-to-agent forwards from the original user request. Bounded by `safety.loop.max_agent_hops`. |

## Execution

| English | 日本語 | Definition |
|---------|--------|------------|
| Context Frame | コンテキストフレーム | The read-only payload built per phase visit and given to the LLM. |
| Control IR | Control IR | List of side-effect ops the LLM may emit (file, ask_user, run_skill, lint, shell). |
| Preprocessor | プリプロセッサ | Deterministic pre-LLM steps a phase may declare (`run_skill`, `iterate`, `validate`, `python`). |
| Decision | 決定 | OS-level value: `continue` / `finish` / `abort`. Never skill-specific. |
| Transition | 遷移 | A move from one phase to another, validated against the skill graph. |
| Final Output | 最終出力 | The artifact produced when a skill finishes; validated against the skill's `final_output` declaration. |
| Visit Count | 訪問回数 | Number of times a single phase has been entered in the current run. |

## DSL files

| File | Purpose |
|------|---------|
| `skill.md` | Skill declaration: `entry`, `graph`, `final_output`, permissions. |
| `phases/<name>.md` | Phase declaration: `input`, optional `preprocessor`, instructions in body. |
| `artifacts/<name>.yaml` | Artifact schema (JSON Schema fragment). |
| `eval.md` | Optional eval spec: cases + per-phase quality criteria. |
| `<name>.py` | Optional Python module for `python` preprocessor steps. |

## Permission verbs

| Verb | Meaning |
|------|---------|
| `allow` | Always permit, no prompt. |
| `ask` | Prompt the user the first time; persist the choice. |
| `deny` | Reject without prompt. |

## Modes

| Mode | Where | Meaning |
|------|-------|---------|
| `safe` | Python preprocessor | AST-validated, sandboxed builtins, allowed-modules-only imports. No extra flag needed. |
| `unsafe` | Python preprocessor | Free Python; requires `--allow-unsafe-python` at the CLI plus a `permissions.python` entry with `mode: unsafe` in `skill.md`. **Stdlib skills in `mode: unsafe` are auto-trusted** — the flag is still required but no approval prompt fires (the skill is vendored with reyn, not user-supplied). |

## DO NOT confuse

- **`continue` (decision) vs "next phase = X" (transition)** — `continue` is OS-level; the actual phase name is in `next_phase`.
- **`finish` (decision) vs `end` (graph token)** — `end` appears in `graph:` adjacency lists to mark terminal transitions; `finish` is the LLM's decision value when terminating.
- **`Skill` vs "skill"** — capitalized when referring to the architectural concept; lowercase in CLI commands and file names.
