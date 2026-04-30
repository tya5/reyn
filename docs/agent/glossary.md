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
| Agent | エージェント | The interpreter of user intent. Selects or generates a Skill. |
| Skill | スキル | A directory defining a phase graph and final output schema. |
| Phase | フェーズ | A reusable processing unit declaring only its `input` and instructions. |
| OS | OS | The runtime executor; sole owner of control flow. |
| Workspace | ワークスペース | The shared store for files and artifacts. |
| Artifact | アーティファクト | Structured data passed between phases. |
| Event | イベント | A recorded state change. |

## Execution

| English | 日本語 | Definition |
|---------|--------|------------|
| Context Frame | コンテキストフレーム | The read-only payload built per phase visit and given to the LLM. |
| Control IR | Control IR | List of side-effect ops the LLM may emit (file, ask_user, run_skill, lint, shell). |
| Preprocessor | プリプロセッサ | Deterministic pre-LLM steps a phase may declare (`run_skill`, `iterate`, `validate`, `python`). |
| Decision | 決定 | OS-level value: `continue` / `finish` / `abort`. Never skill-specific. |
| Transition | 遷移 | A move from one phase to another, validated against the skill graph. |
| Final Output | 最終出力 | The artifact produced when a skill finishes; validated against `final_output_schema`. |
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
| pure | Python preprocessor | AST-validated, sandboxed builtins, allowed-modules-only imports. |
| trusted | Python preprocessor | Free Python; requires `--allow-untrusted-python` plus permission grant. |

## DO NOT confuse

- **`continue` (decision) vs "next phase = X" (transition)** — `continue` is OS-level; the actual phase name is in `next_phase`.
- **`finish` (decision) vs `end` (graph token)** — `end` appears in `graph:` adjacency lists to mark terminal transitions; `finish` is the LLM's decision value when terminating.
- **`Skill` vs "skill"** — capitalized when referring to the architectural concept; lowercase in CLI commands and file names.
