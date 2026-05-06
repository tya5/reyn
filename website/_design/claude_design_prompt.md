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
```
