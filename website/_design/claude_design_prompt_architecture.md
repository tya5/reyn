# Claude Design Prompt — Reyn Architecture Page

## Part A: Before You Start — Upload These Files

Open Claude Design and upload the following brand assets before sending the prompt.
Claude Design will scan them to build the design system automatically.

```
reyn_assets/brand/logo/logo-primary.svg
reyn_assets/brand/background/bg-hero-reyn-light.png
reyn_assets/brand/background/bg-neutral-flow-dark.png
reyn_assets/brand/background/bg-neutral-flow-light.png
reyn_assets/brand/illustration/illustration-agent-flow-primary.png
reyn_assets/brand/illustration/illustration-control-primary.png
reyn_assets/brand/illustration/illustration-flow-primary.png
```

---

## Part B: Prompt (paste this into Claude Design)

~~~xml
<role>
You are a visual designer creating a technical architecture page for an open-source AI product.
</role>

<task>
Design a single-page architecture explainer for Reyn — an open-source AI agent orchestration system.
Deliver an interactive prototype as a single HTML file with inline CSS and embedded Mermaid diagrams.
The page lives at `website/architecture.html` in the same repo as the landing page.
</task>

<context>
Brand identity (derived from uploaded files):
- Product name: Reyn
- Primary color: #C8553D (clay) — use on logo, section accents, CTA, and diagram link colours
- Background: warm off-white #FAF8F6
- Surface card: pure white #FFFFFF with 1px border #E0DBD7
- Body text: #1A1A1A
- Muted text: #6B6B6B
- Design philosophy: subtraction over addition — clean, structured, no decorative gradients
- Audience: engineers who want to understand the internal design of an AI agent OS

Page tone: long-form technical explainer. Think "architecture.md rendered beautifully."
Not a marketing page — a deep technical reference with clear visual diagrams.
</context>

<page_structure>
The page is a vertical scroll composed of the following sections, top to bottom.
Each section carries a unique background treatment (see bg-* assets) and a Mermaid diagram.
Follow this exact order:

HEADER (sticky)
  - Logo top-left (logo-primary.svg tinted #C8553D), nav link "← Landing" to index.html
  - No other nav items

HERO
  - Full-width, bg-hero-reyn-light.png as background
  - Small eyebrow text: {{ARCH_HERO_EYEBROW}}
  - Large H1: {{ARCH_HERO_H1_HTML}}
  - Lede paragraph: {{ARCH_HERO_LEDE}}

SECTION 01 — 全体レイヤー構造 (Overall layer structure)
  - Section number + label on left: {{ARCH_S01_NUM}} / {{ARCH_S01_LABEL}}
  - Heading: {{ARCH_S01_HEADING_HTML}}
  - Body paragraph: {{ARCH_S01_BODY}}
  - Mermaid diagram: full-width, card with 1px border, bg #FFFFFF
    The diagram shows the 7-layer stack from User down to Persistence.
    Diagram code (embed as-is inside a .mermaid div):
    ```
    flowchart TB
        U(["User / External System"])
        subgraph I01["01 · Interface"]
            direction LR
            CLI["CLI"] ~~~ TUI["TUI (Textual)"] ~~~ WEB["Web UI (FastAPI + WebSocket)"]
        end
        subgraph I02["02 · Agent Registry"]
            direction LR
            AM["manages named agents"] ~~~ TG["Topology gate — network · team · pipeline"]
        end
        subgraph I03["03 · Agent = ChatSession"]
            direction LR
            RL["RouterLoop (LLM-driven dispatch)"] ~~~ PL["Planner (optional · plan mode)"] ~~~ MEM["memory scope · skill allowlist"]
        end
        subgraph I04["04 · OS — the constant"]
            direction LR
            CB["Context Build"] ~~~ LC["LLM Call"] ~~~ VA["Output Validation"] ~~~ EE["Event Emit"]
        end
        subgraph I05["05 · Skill"]
            direction LR
            PG["Phase Graph + Transitions"] ~~~ FO["final_output_schema"] ~~~ PP["Postprocessor"]
        end
        subgraph I06["06 · Phase"]
            direction LR
            PRE["Preprocessor"] ~~~ LO["LLM + Control IR ops"] ~~~ LP["↺ loops until transition/finish"]
        end
        subgraph I07["07 · Persistence"]
            direction LR
            WS["Workspace (artifacts · files)"] ~~~ EV["Events (append-only log)"]
        end
        U --> I01 --> I02 --> I03 --> I04 --> I05 --> I06 --> I07
    ```

SECTION 02 — Agent / RouterLoop / Planner
  - Section number + label: {{ARCH_S02_NUM}} / {{ARCH_S02_LABEL}}
  - Heading: {{ARCH_S02_HEADING_HTML}}
  - Two-column layout on desktop (text left, diagram right), single column on mobile
  - Body: {{ARCH_S02_BODY}}
  - Mermaid diagram (Planner flow):
    ```
    sequenceDiagram
        participant U as User
        participant RL as RouterLoop
        participant RLLLM as Router LLM
        participant PL as Planner
        participant SLLM as Step LLM (narrow context)
        U->>RL: complex multi-step request
        RL->>RLLLM: tools (including plan_task)
        RLLLM-->>RL: tool_call: plan_task(goal, steps[])
        RL->>PL: parse_and_validate_plan()
        PL-->>RL: Plan { goal, steps: [S1, S2, S3] }
        loop Each step (dependency order)
            RL->>SLLM: step description + narrow tool catalog
            SLLM-->>RL: step result
        end
        RL->>RLLLM: aggregate all step results
        RLLLM-->>U: final reply
        Note over PL: chat-scoped · transient
    ```

SECTION 03 — OS — the constant (Phase execution flow)
  - Section number + label: {{ARCH_S03_NUM}} / {{ARCH_S03_LABEL}}
  - Heading: {{ARCH_S03_HEADING_HTML}}
  - Full-width diagram first, then body text below
  - Body: {{ARCH_S03_BODY}}
  - Mermaid diagram (Phase execution sequence):
    ```
    sequenceDiagram
        participant OS as OS / Engine
        participant PRE as Preprocessor
        participant LLM as LLM
        participant OPS as Control IR ops
        participant WS as Workspace + Events
        OS->>PRE: run preprocessor steps
        PRE-->>OS: enriched input artifact
        loop act turns (max_act_turns)
            OS->>LLM: context + artifact + allowed_ops + candidate_outputs
            LLM-->>OS: { control, artifact, control_ir[] }
            OS->>OPS: execute control_ir ops
            OPS->>WS: write results
            OPS-->>OS: op results
            alt decision = continue (act)
                Note over OS,LLM: updated context → loop
            else decision = transition
                OS->>WS: emit phase_completed event
                Note over OS: enter next phase
            else decision = finish
                OS->>WS: emit skill_finished event
                Note over OS: Postprocessor (Skill level)
            end
        end
    ```

SECTION 04 — Skill & Phase graph structure
  - Section number + label: {{ARCH_S04_NUM}} / {{ARCH_S04_LABEL}}
  - Heading: {{ARCH_S04_HEADING_HTML}}
  - Two-column layout on desktop (diagram left, text right), single column on mobile
  - Body: {{ARCH_S04_BODY}}
  - Mermaid diagram (Skill graph):
    ```
    flowchart LR
        EP(["entry_phase"])
        subgraph SK["Skill — directed phase graph"]
            PA["Phase A\ninput_schema · preprocessor\ninstructions · allowed_ops"]
            PB["Phase B"]
            PC["Phase C (can_finish)"]
            SUB["@sub_skill node\n(embedded skill)"]
        end
        FO["final_output_schema"]
        POSTP["Postprocessor (optional)"]
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

SECTION 05 — Multi-agent — 4 layers
  - Section number + label: {{ARCH_S05_NUM}} / {{ARCH_S05_LABEL}}
  - Heading: {{ARCH_S05_HEADING_HTML}}
  - Full-width layout: 4 horizontal cards, one per layer, then body text below
  - Each card has a small label (L1 / L2 / L3 / L4) and text: {{ARCH_S05_C1_LABEL}} / {{ARCH_S05_C1_BODY}} ... through C4
  - Body paragraph below cards: {{ARCH_S05_BODY}}
  - Mermaid diagram (A2A delegation chain):
    ```
    sequenceDiagram
        participant U as User (depth=0)
        participant A as Agent A (depth=1)
        participant B as Agent B (depth=2)
        participant C as Agent C (depth=3=limit)
        U->>A: message
        Note over A: RouterLoop → delegate_to_agent
        A->>B: send(to=B, depth=1, chain_id=xyz)
        Note over B: RouterLoop → delegate_to_agent
        B->>C: send(to=C, depth=2, chain_id=xyz)
        Note over C: depth=3 = max_hop_depth
        C-->>B: result (chain_id=xyz)
        B-->>A: result (chain_id=xyz)
        A-->>U: final reply
        Note over A,C: chain timeout = 60s
    ```

SECTION 06 — MCP server / client
  - Section number + label: {{ARCH_S06_NUM}} / {{ARCH_S06_LABEL}}
  - Heading: {{ARCH_S06_HEADING_HTML}}
  - Two-column layout on desktop: left = MCP server card, right = MCP client card
  - Left card body: {{ARCH_S06_SERVER_BODY}}
  - Right card body: {{ARCH_S06_CLIENT_BODY}}
  - Mermaid diagram (MCP symmetry):
    ```
    flowchart LR
        subgraph SRV["MCP Server — implemented (reyn mcp serve)"]
            direction TB
            EXT["External LLM Client\nClaude Code · Cursor · OpenAI SDK"]
            STDIO["stdio / JSON-RPC 2.0"]
            TOOLS["list_agents() · send_to_agent()"]
            AR["AgentRegistry → ChatSession"]
            EXT --> STDIO --> TOOLS --> AR
        end
        subgraph CLI2["MCP Client — Phase 2 roadmap"]
            direction TB
            PHASE["Reyn Phase (control_ir)"]
            MCIROP["MCPIROp  kind: mcp"]
            MCLIENT["MCPClient\nstdio / HTTP transport"]
            EXTMCP["External MCP Server"]
            PHASE --> MCIROP --> MCLIENT --> EXTMCP
        end
    ```

CTA (bottom)
  - Centred, dark background (bg-neutral-flow-dark.png)
  - Heading: {{ARCH_CTA_HEADING_HTML}}
  - Body: {{ARCH_CTA_BODY}}
  - Primary button "Read the concepts →" links to {{ARCH_CTA_DOCS_HREF}}
  - Secondary link "← Back to home" links to index.html

FOOTER
  - Same footer as index.html: {{FOOTER_LINE}} (reuse existing token)
</page_structure>

<visual_treatment>
Section backgrounds (alternate to create rhythm):
- HERO: bg-hero-reyn-light.png
- S01, S03, S05: bg-neutral-flow-light.png (subtle warm off-white texture)
- S02, S04, S06: plain white #FFFFFF
- CTA: bg-neutral-flow-dark.png

Diagram cards:
- Background: #FFFFFF
- Border: 1px solid #E0DBD7
- Border-radius: 4px
- Padding: 24px
- Mermaid theme: base, with themeVariables primaryColor:#FAF8F6, lineColor:#C8553D, fontFamily: Inter

Section layout pattern:
- Section number: small, muted, #6B6B6B, monospace
- Section label: uppercase, letter-spacing: 0.1em, #6B6B6B, small
- Section divider: 1px solid #E0DBD7
- Heading: large, font-weight 500, #1A1A1A
- Body: medium, line-height 1.7, #4A4A4A, max-width 60ch for readability

Typography scale (consistent with landing page):
- H1: 48-56px
- Section heading: 28-32px
- Body: 16-17px
- Label/eyebrow: 11-12px uppercase tracked

Responsive: all two-column layouts collapse to single column below 768px.
</visual_treatment>

<constraints>
- No decorative gradients or drop shadows
- Large negative space — sections breathe; min padding-top/bottom 80px per section
- No flashy transitions or animations
- Mermaid is loaded from CDN: https://cdn.jsdelivr.net/npm/mermaid@11/dist/mermaid.min.js
- Output: single self-contained architecture.html with all CSS inlined
- The page must share the same visual language as index.html (same fonts, same colors, same component patterns)
</constraints>

<copy_placeholder_convention>
All visible body copy must use {{TOKEN}} placeholders, never literal copy.
The build script `website/build.py` substitutes them from `website/copy.yaml`.

New tokens for this page (use ARCH_ prefix to avoid collision with index.html tokens):

HERO:
- {{ARCH_PAGE_TITLE}}        — <title> text
- {{ARCH_HERO_EYEBROW}}      — small uppercase eyebrow above H1
- {{ARCH_HERO_H1_HTML}}      — main H1 (HTML allowed, wrap accents in <em>)
- {{ARCH_HERO_LEDE}}         — paragraph below H1

Section 01 (Layer structure):
- {{ARCH_S01_NUM}}            — "01"
- {{ARCH_S01_LABEL}}          — section label
- {{ARCH_S01_HEADING_HTML}}   — section heading
- {{ARCH_S01_BODY}}           — body paragraph

Section 02 (Agent / RouterLoop / Planner):
- {{ARCH_S02_NUM}}, {{ARCH_S02_LABEL}}, {{ARCH_S02_HEADING_HTML}}, {{ARCH_S02_BODY}}

Section 03 (OS / Phase execution):
- {{ARCH_S03_NUM}}, {{ARCH_S03_LABEL}}, {{ARCH_S03_HEADING_HTML}}, {{ARCH_S03_BODY}}

Section 04 (Skill & Phase graph):
- {{ARCH_S04_NUM}}, {{ARCH_S04_LABEL}}, {{ARCH_S04_HEADING_HTML}}, {{ARCH_S04_BODY}}

Section 05 (Multi-agent):
- {{ARCH_S05_NUM}}, {{ARCH_S05_LABEL}}, {{ARCH_S05_HEADING_HTML}}, {{ARCH_S05_BODY}}
- {{ARCH_S05_C1_LABEL}}, {{ARCH_S05_C1_BODY}}  — Layer 1 card (@sub_skill)
- {{ARCH_S05_C2_LABEL}}, {{ARCH_S05_C2_BODY}}  — Layer 2 card (run_skill)
- {{ARCH_S05_C3_LABEL}}, {{ARCH_S05_C3_BODY}}  — Layer 3 card (delegate_to_agent)
- {{ARCH_S05_C4_LABEL}}, {{ARCH_S05_C4_BODY}}  — Layer 4 card (reyn mcp serve)

Section 06 (MCP):
- {{ARCH_S06_NUM}}, {{ARCH_S06_LABEL}}, {{ARCH_S06_HEADING_HTML}}
- {{ARCH_S06_SERVER_BODY}}   — MCP server card body
- {{ARCH_S06_CLIENT_BODY}}   — MCP client card body

CTA:
- {{ARCH_CTA_HEADING_HTML}}  — CTA heading
- {{ARCH_CTA_BODY}}          — CTA body paragraph
- {{ARCH_CTA_DOCS_HREF}}     — docs link target

Reused from index.html (already in copy.yaml — do not rename):
- {{FOOTER_LINE}}
- {{NAV_GITHUB_HREF}}

Rules:
1. Place placeholders inside the natural HTML element (e.g. <h2>{{ARCH_S01_HEADING_HTML}}</h2>).
2. Never hard-code literal copy anywhere — every visible string must be a token.
3. Do not invent names that overlap existing tokens (S01_*, S02_*, etc. are taken by index.html).
4. Do not write fallback copy as placeholder defaults.
</copy_placeholder_convention>
~~~
