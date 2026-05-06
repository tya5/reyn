# Claude Design Prompt — MkDocs Material Custom CSS

## Part A: Before You Start — Upload These Files

Upload the following files so Claude Design can read the existing design system:

```
website/assets/logo-primary.svg
website/index.html               ← LP design (visual reference)
reyn_assets/prompt/design_site.md  ← brand identity reference
```

Paste the content of `colors_and_type.css` inline in the prompt (Part B below already includes it).

---

## Part B: Prompt (paste this into Claude Design)

```xml
<role>
You are a CSS engineer customizing mkdocs-material to match an existing brand design system.
</role>

<task>
Produce two files that apply the Reyn brand to a mkdocs-material documentation site:
1. docs/stylesheets/extra.css — CSS overrides for mkdocs-material
2. The mkdocs.yml palette block (replacement snippet only)
</task>

<context>
The brand design system tokens are:

Primary accent: #C8553D (--reyn-clay-500)
Page background: #FFFFFF
Soft background: #FCFBFA
Muted background: #F7F6F4
Primary text: #131211
Body text: #232120
Secondary text: #5A5651
Tertiary text: #807B74
Border color: #E2DFDB

Font sans: 'Geist', ui-sans-serif, -apple-system, BlinkMacSystemFont, 'Helvetica Neue', Arial, sans-serif
Font mono: 'Geist Mono', ui-monospace, 'SF Mono', Menlo, Consolas, monospace

Design rules:
- No shadows (elevation = hairline borders only)
- No decorative gradients
- Accent color used sparingly: links, active nav items, inline code highlights
- Warm-leaning neutral grays throughout
- Dark mode: background #0E0D0C, text #EFEDEA

The landing page (index.html, uploaded) shows the visual target.

mkdocs-material CSS custom properties to override:
--md-primary-fg-color        → #C8553D
--md-primary-fg-color--light → #EAB1A1
--md-primary-fg-color--dark  → #863526
--md-accent-fg-color         → #C8553D
--md-default-fg-color        → #232120
--md-default-bg-color        → #FFFFFF
--md-text-font               → Geist
--md-code-font               → Geist Mono
</context>

<constraints>
- extra.css must load Geist and Geist Mono from Google Fonts
- Override mkdocs-material variables under [data-md-color-scheme="default"] for light mode
- Override under [data-md-color-scheme="slate"] for dark mode using bg-dark (#0E0D0C) and fg-on-dark (#EFEDEA)
- Do not override layout structure — only colors, fonts, and typography
- No !important unless mkdocs-material specifically requires it
- The mkdocs.yml palette block should remove the primary/accent color keys (let CSS control instead) and keep the light/dark toggle
- Output: extra.css in full, then the mkdocs.yml palette snippet
</constraints>
```
