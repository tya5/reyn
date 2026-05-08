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
Design a single-page architecture overview for Reyn — an open-source AI agent
orchestration system. Deliver an interactive prototype as a single HTML file
with inline CSS and embedded Mermaid diagrams. The page lives at
`website/architecture.html` in the same repo as the landing page.
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
- Audience: engineers visiting Reyn for the first time who want a bird's-eye view

Page purpose: give a first-time visitor a clear mental model of what Reyn is
made of and how those pieces coordinate to execute one request. Depth lives
in the docs — this page is the overview that makes the docs worth reading.

Tone: confident, declarative, structural. No marketing fluff, no excessive
detail. Each section answers one question.
</context>

<page_structure>
The page is a vertical scroll composed of HEADER, HERO, four content
sections, a CTA, and a FOOTER. Follow this exact order.

HEADER (sticky)
  - Logo top-left (logo-primary.svg tinted #C8553D)
  - Single nav link "← Landing" pointing to index.html
  - No other nav items

HERO
  - Full-width, bg-hero-reyn-light.png as background
  - Small eyebrow text: {{ARCH_HERO_EYEBROW}}
  - Large H1: {{ARCH_HERO_H1_HTML}}
  - Lede paragraph: {{ARCH_HERO_LEDE}}

SECTION 01 — At a glance
  Question answered: "What is Reyn made of?"
  - Section number + label on left: {{ARCH_S01_NUM}} / {{ARCH_S01_LABEL}}
  - Heading: {{ARCH_S01_HEADING_HTML}}
  - Body paragraph: {{ARCH_S01_BODY}}
  - One large Mermaid diagram below the body, full-width white card with
    1px border #E0DBD7. This is THE overview diagram of the page.
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

SECTION 02 — The pieces
  Question answered: "What does each component do?"
  - Section number + label: {{ARCH_S02_NUM}} / {{ARCH_S02_LABEL}}
  - Heading: {{ARCH_S02_HEADING_HTML}}
  - Body paragraph: {{ARCH_S02_BODY}}
  - Five component cards, arranged in a responsive grid (3 columns on
    desktop, 2 on tablet, 1 on mobile). Each card:
    - Small label in clay (#C8553D) — e.g. "Agent"
    - Short title-card body — 2 to 3 sentences, body weight
    - Subtle 1px border, 4px radius, 24px padding
  - Card 1: {{ARCH_S02_C1_LABEL}} / {{ARCH_S02_C1_BODY}}
  - Card 2: {{ARCH_S02_C2_LABEL}} / {{ARCH_S02_C2_BODY}}
  - Card 3: {{ARCH_S02_C3_LABEL}} / {{ARCH_S02_C3_BODY}}
  - Card 4: {{ARCH_S02_C4_LABEL}} / {{ARCH_S02_C4_BODY}}
  - Card 5: {{ARCH_S02_C5_LABEL}} / {{ARCH_S02_C5_BODY}}
  - No diagram — the cards ARE the visual

SECTION 03 — A request, end to end
  Question answered: "How do these pieces work together?"
  - Section number + label: {{ARCH_S03_NUM}} / {{ARCH_S03_LABEL}}
  - Heading: {{ARCH_S03_HEADING_HTML}}
  - Mermaid sequence diagram first, then body paragraph below
  - Body: {{ARCH_S03_BODY}}
  - Diagram shows one user message flowing all the way through the system
    and back out as a reply. This is the climax of the page — the
    "everything connects" moment.
    Diagram code:
    ```
    sequenceDiagram
        participant U as User
        participant A as Agent (ChatSession)
        participant SK as Skill graph
        participant PH as Phase loop
        participant WS as Workspace + Events
        U->>A: message
        A->>A: RouterLoop picks Skill
        A->>SK: invoke skill
        SK->>PH: enter entry phase
        loop Until transition or finish
            PH->>PH: preprocessor (deterministic)
            PH->>PH: LLM call (closed candidate set)
            PH->>PH: validate output against schema
            PH->>WS: execute Control IR ops · emit events
            PH-->>SK: result + control decision
            alt decision = transition
                Note over SK: validate against next.input_schema
                SK->>PH: enter next phase
            else decision = finish
                Note over SK: validate against final_output_schema
                SK-->>A: final output
            end
        end
        A-->>U: reply
    ```

SECTION 04 — Beyond a single agent
  Question answered: "What happens when one agent isn't enough?"
  - Section number + label: {{ARCH_S04_NUM}} / {{ARCH_S04_LABEL}}
  - Heading: {{ARCH_S04_HEADING_HTML}}
  - Body paragraph: {{ARCH_S04_BODY}}
  - Two diagrams side by side on desktop, stacked on mobile.
    Left = A2A delegation chain. Right = MCP server/client symmetry.
    Each in its own white card with 1px border.
    Left diagram code:
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
    Right diagram code:
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
        subgraph CLI2["MCP Client — implemented (stdio · HTTP)"]
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
Section background rhythm (keep readers oriented):
- HERO: bg-hero-reyn-light.png
- S01, S03: bg-neutral-flow-light.png (subtle warm off-white texture)
- S02, S04: plain white #FFFFFF
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

Responsive: all multi-column layouts collapse to single column below 768px.
</visual_treatment>

<constraints>
- No decorative gradients or drop shadows
- Large negative space — sections breathe; min padding-top/bottom 80px per section
- No flashy transitions or animations
- Mermaid is loaded from CDN: https://cdn.jsdelivr.net/npm/mermaid@11/dist/mermaid.min.js
- Output: single self-contained architecture.html with all CSS inlined
- The page must share the same visual language as index.html (same fonts, same colors, same component patterns)
- The page must be SHORT enough to read in 2 minutes — depth lives in the docs
</constraints>

<copy_placeholder_convention>
All visible body copy must use {{TOKEN}} placeholders, never literal copy.
The build script `website/build.py` substitutes them from `website/copy.yaml`.

Tokens for this page (ARCH_ prefix to avoid collision with index.html tokens):

HERO:
- {{ARCH_PAGE_TITLE}}        — <title> text
- {{ARCH_HERO_EYEBROW}}      — small uppercase eyebrow above H1
- {{ARCH_HERO_H1_HTML}}      — main H1 (HTML allowed, wrap accents in <em>)
- {{ARCH_HERO_LEDE}}         — paragraph below H1

Section 01 (At a glance):
- {{ARCH_S01_NUM}}            — "01"
- {{ARCH_S01_LABEL}}          — section label
- {{ARCH_S01_HEADING_HTML}}   — section heading
- {{ARCH_S01_BODY}}           — body paragraph

Section 02 (The pieces):
- {{ARCH_S02_NUM}}, {{ARCH_S02_LABEL}}, {{ARCH_S02_HEADING_HTML}}, {{ARCH_S02_BODY}}
- {{ARCH_S02_C1_LABEL}}, {{ARCH_S02_C1_BODY}}  — Agent card
- {{ARCH_S02_C2_LABEL}}, {{ARCH_S02_C2_BODY}}  — Skill card
- {{ARCH_S02_C3_LABEL}}, {{ARCH_S02_C3_BODY}}  — Phase card
- {{ARCH_S02_C4_LABEL}}, {{ARCH_S02_C4_BODY}}  — OS card
- {{ARCH_S02_C5_LABEL}}, {{ARCH_S02_C5_BODY}}  — State card

Section 03 (A request, end to end):
- {{ARCH_S03_NUM}}, {{ARCH_S03_LABEL}}, {{ARCH_S03_HEADING_HTML}}, {{ARCH_S03_BODY}}

Section 04 (Beyond a single agent):
- {{ARCH_S04_NUM}}, {{ARCH_S04_LABEL}}, {{ARCH_S04_HEADING_HTML}}, {{ARCH_S04_BODY}}

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
