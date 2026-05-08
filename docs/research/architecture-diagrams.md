---
title: Architecture Diagrams (Mermaid)
status: draft
date: 2026-05-08
---

# Reyn Architecture Diagrams

全図 Mermaid。mkdocs-material でそのまま埋め込み可。

---

## 1. 全体レイヤー構造

```mermaid
flowchart TB
    U(["User / External System"])

    subgraph I01["01 · Interface"]
        direction LR
        CLI["CLI"] & TUI["TUI\n(Textual)"] & WEB["Web UI\n(FastAPI + WebSocket)"]
    end

    subgraph I02["02 · Agent Registry"]
        direction LR
        AM["manages named agents"] & TG["Topology gate\nnetwork · team · pipeline"]
    end

    subgraph I03["03 · Agent  =  ChatSession"]
        direction LR
        RL["RouterLoop\n(LLM-driven dispatch)"] & PL["Planner\n(optional · plan mode)"]
        MEM["memory scope · skill allowlist"]
    end

    subgraph I04["04 · OS — the constant"]
        direction LR
        CB["Context\nBuild"] & LC["LLM\nCall"] & VA["Output\nValidation"] & EE["Event\nEmit"]
    end

    subgraph I05["05 · Skill"]
        direction LR
        PG["Phase Graph\n+ Transitions"] & FO["final_output_schema"] & PP["Postprocessor"]
    end

    subgraph I06["06 · Phase"]
        direction LR
        PRE["Preprocessor"] & LO["LLM + Control IR ops"] & LP["↺ loops until\ntransition / finish"]
    end

    subgraph I07["07 · Persistence"]
        direction LR
        WS["Workspace\n(artifacts · files)"] & EV["Events\n(append-only log)"]
    end

    U --> I01 --> I02 --> I03 --> I04 --> I05 --> I06 --> I07
```

---

## 2. Phase 実行シーケンス

```mermaid
sequenceDiagram
    participant OS as OS / Engine
    participant PRE as Preprocessor
    participant LLM as LLM
    participant OPS as Control IR ops
    participant WS as Workspace + Events

    OS->>PRE: run preprocessor steps
    PRE-->>OS: enriched input artifact

    loop act turns  (max_act_turns)
        OS->>LLM: context + artifact + allowed_ops + candidate_outputs
        LLM-->>OS: { control, artifact, control_ir[] }
        OS->>OPS: execute control_ir ops
        OPS->>WS: write results (P5)
        OPS-->>OS: op results

        alt decision = continue  (act)
            Note over OS,LLM: updated context → loop
        else decision = transition
            OS->>WS: emit phase_completed event (P6)
            Note over OS: enter next phase
        else decision = finish
            OS->>WS: emit skill_finished event (P6)
            Note over OS: → Postprocessor (Skill level)
        end
    end
```

---

## 3. Skill グラフ構造

```mermaid
flowchart LR
    EP(["entry_phase"])

    subgraph SK["Skill — directed phase graph"]
        PA["Phase A\ninput_schema\npreprocessor steps\ninstructions\nallowed_ops"]
        PB["Phase B"]
        PC["Phase C\n(can_finish)"]
        SUB["@sub_skill node\n(embedded skill)"]
    end

    FO["final_output_schema"]
    POSTP["Postprocessor\n(optional)"]
    OUT(["Output to caller"])

    EP --> PA
    PA -->|"transition: proceed"| PB
    PA -->|"transition: delegate"| SUB
    SUB -->|"result"| PB
    PB -->|"transition: revise"| PA
    PB -->|"transition: proceed"| PC
    PC -->|"finish"| FO
    FO --> POSTP
    POSTP --> OUT
```

---

## 4. Control IR ops フロー

```mermaid
flowchart TB
    subgraph FRONT["Two frontends — one shared backend"]
        direction LR
        subgraph STATIC["Preprocessor  (static · before LLM)"]
            PS["PreprocessorExecutor"]
        end
        subgraph DYNAMIC["Control IR  (dynamic · LLM-driven)"]
            CD["ControlIRExecutor"]
        end
    end

    GATE["Permission Gate\n(PermissionResolver)"]
    EX["execute_op()"]

    subgraph HANDLERS["Handler Registry"]
        direction LR
        FILE["file\nread · write · edit\ngrep · glob · delete"]
        WEB["web_search\nweb_fetch"]
        SHELL["shell"]
        ASK["ask_user\n※ControlIR only"]
        RS["run_skill"]
        LINT["lint"]
        MCP["mcp"]
    end

    PS --> GATE
    CD --> GATE
    GATE --> EX
    EX --> FILE & WEB & SHELL & ASK & RS & LINT & MCP

    style ASK stroke-dasharray: 4
```

---

## 5. Preprocessor / Postprocessor 対称図

```mermaid
flowchart LR
    subgraph PRE["PREPROCESSOR  ·  Phase level"]
        direction TB
        PI["Phase input artifact"]
        PSTEPS["run_op\niterate\nvalidate\nlint_plan\npython"]
        PO["Enriched artifact → LLM"]
        PI --> PSTEPS --> PO
        PTIMING(["fires before LLM call"])
    end

    subgraph POST["POSTPROCESSOR  ·  Skill level"]
        direction TB
        QI["final_output artifact\n(final_output_schema)"]
        QSTEPS["run_op\niterate\nvalidate\nlint_plan\npython\n+ WAL memoization"]
        QO["Caller-facing artifact\n(postprocessor.output_schema)"]
        QI --> QSTEPS --> QO
        QTIMING(["fires after all phases finish"])
    end
```

---

## 6. マルチエージェント 4 層構造

```mermaid
flowchart TB
    TITLE["Reyn multi-agent mechanisms"]

    subgraph L1["Layer 1 · @sub_skill — static"]
        S1["Skill graph に埋め込まれた sub-skill ノード\nコンパイル時に解決 · 親スキルのグラフの一部"]
    end

    subgraph L2["Layer 2 · run_skill op — dynamic"]
        S2["Phase の control_ir から動的にサブスキルを呼び出す\nparent_run_id で系譜追跡 · isolated/shared workspace 選択可"]
    end

    subgraph L3["Layer 3 · delegate_to_agent — A2A"]
        S3["RouterLoop が delegate_to_agent を発行\nAgentRegistry が Topology gate でルーティング\nhop depth ≤ 3 · chain_id でスレッド追跡 · timeout 60s"]
    end

    subgraph L4["Layer 4 · reyn mcp serve — external"]
        S4["外部 LLM クライアント (Claude Code / Cursor / OpenAI SDK)\nが stdio/JSON-RPC で Reyn を MCP server として呼ぶ\nlist_agents() · send_to_agent()"]
    end

    TITLE --> L1 & L2 & L3 & L4
```

---

## 7. A2A 委譲チェーン

```mermaid
sequenceDiagram
    participant U as User (depth=0)
    participant A as Agent A (depth=1)
    participant B as Agent B (depth=2)
    participant C as Agent C (depth=3 = limit)

    U->>A: message
    Note over A: RouterLoop → delegate_to_agent
    A->>B: send(to=B, depth=1, chain_id=xyz)
    Note over B: RouterLoop → delegate_to_agent
    B->>C: send(to=C, depth=2, chain_id=xyz)
    Note over C: depth=3 = max_hop_depth\n以上の委譲は拒否
    C-->>B: result (chain_id=xyz)
    B-->>A: result (chain_id=xyz)
    A-->>U: final reply

    Note over A,C: chain timeout = 60s
```

---

## 8. MCP server / client 対称図

```mermaid
flowchart LR
    subgraph SRV["MCP Server — 実装済み  (reyn mcp serve)"]
        direction TB
        EXT["External LLM Client\nClaude Code · Cursor · OpenAI SDK"]
        STDIO["stdio / JSON-RPC 2.0"]
        TOOLS["list_agents()\nsend_to_agent()"]
        AR["AgentRegistry → ChatSession"]
        EXT --> STDIO --> TOOLS --> AR
    end

    subgraph CLI["MCP Client — implemented (stdio · HTTP)"]
        direction TB
        PHASE["Reyn Phase\n(control_ir)"]
        MCIROP["MCPIROp\nkind: mcp"]
        MCLIENT["MCPClient\nstdio / HTTP transport"]
        EXTMCP["External MCP Server\n(任意の MCP 対応ツール)"]
        PHASE --> MCIROP --> MCLIENT --> EXTMCP
    end
```

---

## 9. Memory スコープ

```mermaid
flowchart TB
    subgraph ROOT[".reyn/  (project root)"]
        direction TB

        subgraph SHARED[".reyn/memory/  — プロジェクト共有"]
            SM["MEMORY.md (index)\n+ &lt;slug&gt;.md  × n\n(type: user / feedback / project / reference)"]
        end

        subgraph AGENTS[".reyn/agents/"]
            subgraph A1["agents/alice/memory/  — エージェント個別"]
                AM1["MEMORY.md + &lt;slug&gt;.md"]
            end
            subgraph A2["agents/bob/memory/  — エージェント個別"]
                AM2["MEMORY.md + &lt;slug&gt;.md"]
            end
        end

        subgraph WSP[".reyn/artifacts/  — Workspace (≠ Memory)"]
            WSN["per-run artifacts\nスキル実行中のみ · 永続化対象外"]
        end
    end

    LLM["LLM\n(Phase 実行中)"]
    LLM -->|"file/write\nControl IR op"| SHARED
    LLM -->|"file/write\nControl IR op"| A1
    LLM -->|"file/write\nControl IR op"| A2
```

---

## 10. Planner フロー

```mermaid
sequenceDiagram
    participant U as User
    participant RL as RouterLoop
    participant RLLLM as Router LLM
    participant PL as Planner
    participant SLLM as Step LLM (narrow context)

    U->>RL: 複雑な複数ステップのリクエスト
    RL->>RLLLM: tools (including plan_task)
    RLLLM-->>RL: tool_call: plan_task(goal, steps[])
    RL->>PL: parse_and_validate_plan()
    PL-->>RL: Plan { goal, steps: [S1, S2, S3] }

    loop 各ステップ (依存関係順)
        RL->>SLLM: step description + narrow tool catalog
        SLLM-->>RL: step result
    end

    RL->>RLLLM: 全ステップ結果を集約
    RLLLM-->>U: final reply

    Note over PL: chat-scoped · transient\nWorkspace には永続化しない
```
