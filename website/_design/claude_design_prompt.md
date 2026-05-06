# Claude Design Prompt — Reyn Landing Page

## Part A: Before You Start — Upload These Files

Open Claude Design and upload the following brand assets before sending the prompt.
Claude Design will scan them to build the design system automatically.

```
reyn_assets/brand/logo/logo-primary.svg
reyn_assets/brand/background/bg-hero-reyn-light.png
reyn_assets/brand/background/bg-neutral-flow-dark.png
reyn_assets/brand/background/bg-neutral-flow-light.png
reyn_assets/brand/banner/banner-hero-reyn-main.png
reyn_assets/brand/illustration/illustration-agent-flow-primary.png
reyn_assets/brand/illustration/illustration-control-primary.png
reyn_assets/brand/illustration/illustration-flow-primary.png
```

---

## Part B: Prompt (paste this into Claude Design)

```xml
<role>
You are a visual designer creating a minimal marketing website for an open-source AI product.
</role>

<task>
Design a landing page for Reyn — an open-source AI agent orchestration system.
Deliver an interactive prototype as a single HTML file with inline CSS.
</task>

<context>
Brand identity (derived from uploaded files):
- Product name: Reyn
- Tagline: "Gives you the reins."
- Primary color: #C8553D — use sparingly on logo, CTA button, and key highlights only
- Palette: white background, neutral grays for text and dividers, #C8553D as sole accent
- Design philosophy: subtraction over addition — remove every element until the design breaks, then add one thing back
- Audience: engineers and developers building AI agent systems

Page structure (5 sections, top to bottom):
1. Hero — logo top-left, large title "Reyn", tagline "Gives you the reins." below, one CTA button "Get Started", full-width background using bg-hero-reyn-light.png
2. What is Reyn? — two-sentence paragraph: Reyn is an AI agent orchestration system. It gives you structured control over multiple agents through defined flows and phases.
3. How it works — illustration-agent-flow-primary.png on one side, two-sentence explanation on the other: Reyn connects agents through a skill graph. Each phase is driven by LLM decisions within a constrained, auditable runtime.
4. Key Concepts — three equal cards labeled Agent, Flow, Control with one-line description each
5. Get Started — CLI install snippet (pip install reyn), link text "Read the docs →"
</context>

<constraints>
- No decorative gradients, no drop shadows, no border-radius abuse
- Large negative space — sections must breathe; never crowd elements
- Responsive layout: stack vertically on mobile, side-by-side on desktop
- No flashy transitions or animations
- Typography: large heading, medium subheading, small readable body copy
- Color accent (#C8553D) appears on: logo tint if needed, CTA button, section divider accents only
- Output: single self-contained index.html with all CSS inlined
</constraints>

<copy_placeholder_convention>
Body copy is maintained separately in `website/copy.yaml` and substituted
into the page at build time by `website/build.py`. The HTML you output
must use `{{TOKEN}}` placeholders for all visible body text — never
literal copy. This lets us regenerate the design without re-editing the
real copy.

Use these exact token names (they map 1:1 onto keys in `copy.yaml`):

- `{{PAGE_TITLE}}` — `<title>` text
- `{{NAV_DOCS}}`, `{{NAV_GITHUB}}`, `{{NAV_GITHUB_HREF}}` — primary nav
- `{{HERO_EYEBROW}}` — small uppercase tagline above the H1
- `{{HERO_H1_HTML}}` — main H1 (HTML allowed — wrap accent words in `<em>`)
- `{{HERO_LEDE}}` — paragraph below the H1
- `{{HERO_CTA_PRIMARY}}`, `{{HERO_CTA_SECONDARY}}` — button labels
- `{{S01_NUM}}`, `{{S01_LABEL}}`, `{{S01_BODY_HTML}}` — "What is Reyn"
- `{{S02_NUM}}`, `{{S02_LABEL}}`, `{{S02_HEADING_HTML}}`, `{{S02_BODY_P1}}`, `{{S02_BODY_P2}}` — "How it works"
- `{{S03_NUM}}`, `{{S03_LABEL}}`, `{{S03_HEADING_HTML}}` — "Key concepts" header
- `{{S03_C1_LABEL}}`, `{{S03_C1_TITLE}}`, `{{S03_C1_BODY}}` — concept card 1
- `{{S03_C2_LABEL}}`, `{{S03_C2_TITLE}}`, `{{S03_C2_BODY}}` — concept card 2
- `{{S03_C3_LABEL}}`, `{{S03_C3_TITLE}}`, `{{S03_C3_BODY}}` — concept card 3
- `{{S04_NUM}}`, `{{S04_LABEL}}`, `{{S04_HEADING_HTML}}`, `{{S04_DOCS_LINK}}`, `{{S04_INSTALL_CMD}}` — "Get started"

Rules:
1. Place placeholders inside the natural HTML element where copy belongs
   (e.g. `<h1>{{HERO_H1_HTML}}</h1>`, not as attribute values).
2. If a section calls for emphasised words rendered in the brand clay
   colour, leave that emphasis to the YAML value — wrap with `<em>`
   inside the YAML, not in the HTML around the placeholder.
3. If you introduce a new copy slot, name it consistently
   (`{{S05_LABEL}}`, etc.) and tell the maintainer to add the matching
   key to `copy.yaml`. Do not invent names that overlap existing ones.
4. Do not write fallback copy as a placeholder default. The build script
   warns about missing keys; we want to catch additions explicitly.
5. Header / footer / nav copy and link `href`s also go through tokens
   (e.g. `{{NAV_GITHUB_HREF}}` for the GitHub URL). Don't hard-code
   `https://github.com/...` URLs.
</copy_placeholder_convention>
```
