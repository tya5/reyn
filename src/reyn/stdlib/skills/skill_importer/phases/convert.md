---
type: phase
name: convert
input: selected_candidate
role: skill_converter
can_finish: true
max_act_turns: 6
allowed_ops: [file, lint, web_fetch]
---

Fetch the chosen source markdown, decompose it into a multi-phase reyn
skill, write the files, lint the result, and report back.

## Step 1 — Fetch the source

In one act turn, emit a `web_fetch` op for `selected_candidate.data.source_url`.

The body of the fetched markdown is the skill's instructions. Some sources
also have YAML frontmatter (`---` block at the top) with `name`,
`description`, etc. — extract those if present.

## Step 2 — Decompose

Read the source carefully. Anthropic-style skills come in two flavours,
and the right decomposition depends on which one you're looking at:

### Reference-format skills → **single phase by default**

Skills like ``pdf`` / ``docx`` / ``xlsx`` / ``claude-api`` are written as
**reference manuals**: a list of operations (read / write / merge /
split / OCR / fill form / ...) the LLM selects from based on the user's
intent. The user dispatches to ONE operation per invocation; the
"operations" do not form a sequential workflow that produces intermediate
artifacts for the next step.

For these, **keep the import as a single phase**. The user's request
("summarize this PDF" vs "merge these PDFs" vs "OCR this scan") routes
to the right code path inside that one phase. Splitting into
``read → process → output`` is mechanical decomposition that **does not
match the source** — it manufactures phase boundaries the original
author never intended and turns inter-operation switching (= a natural
single-phase concern) into broken inter-phase artifact passing.

Tell-tale signs the source is reference-format:

- The body is dominated by code-block "how to do X" recipes, often with
  multiple unrelated recipes (merge / split / OCR / encrypt / ...).
- The recipes are independent; no recipe consumes another recipe's
  output as a prerequisite.
- A table of contents lists "operations" rather than "stages".
- The frontmatter ``description`` enumerates triggers
  ("This includes reading, merging, splitting, ...") — that's the
  router-facing dispatch surface, not a phase sequence.

### Workflow-format skills → **2-4 phases, only when the source explicitly stages**

Skills like ``doc-coauthoring`` / ``brand-guidelines`` (= where the
source describes "first interview the user, then draft, then review")
are workflow-format. Their decomposition IS a sequential phase graph
producing intermediate artifacts.

For these, split into **2-4 phases** at the stages the source explicitly
names. Tell-tale signs:

- The source uses imperative stage markers: "Step 1 — Interview", "Step
  2 — Draft", "Step 3 — Review".
- Each stage's output is described as feeding the next ("the draft is
  then handed to the reviewer").
- The body shows pipeline-style information flow, not a recipe menu.

### Decision rule (= deterministic signals first)

Before counting phases, check these **hard signals** on the source's
frontmatter and body:

1. **Trigger-enumerating description** — does the frontmatter
   ``description`` field contain a phrase like "This includes <list of
   operations>" or "When the user wants to <op1>, <op2>, <op3>, ..."?
   That pattern is Anthropic's canonical reference-format marker. The
   description is enumerating operations the user dispatches between,
   not stages of a pipeline. **→ Single phase.**

2. **No explicit "Step 1 / Step 2 / Step 3" stage markers in the body**
   — workflow-format skills have section headings like ``## Step 1 —
   Interview`` or ``## Stage 1 — Plan``. Reference-format skills have
   section headings like ``## Python Libraries`` or ``## Quick Start``
   or ``## Merging PDFs``. Topical / category headings are NOT stage
   markers. **→ Single phase unless explicit stage markers exist.**

3. **Recipe independence** — open the source body. Does recipe N+1's
   code consume recipe N's output as a prerequisite (= workflow), or
   is each recipe self-contained for a different user intent (=
   reference)? **→ Single phase if recipes are independent.**

If **any one** of signals 1, 2, 3 points at reference-format, the answer
is **single phase**. No quorum, no override. "But it has a lot of
content" / "but I can split it for clarity" / "but recipes can be
grouped by stage" are NOT valid reasons to split — they all manufacture
structure the source doesn't have.

**Imperative**: when you detect signal 1 (= trigger-enumerating
description), you MUST emit ``graph: { <single_phase>: [] }``. Do
NOT use topical body headings (= ``## Python Libraries``,
``## Command-Line Tools``, ``## Common Tasks``) to justify a split —
those are categories of recipes, not pipeline stages. If you find
yourself reasoning "the description suggests reference-format BUT the
body structure suggests phases", you are about to make the wrong
decision. The description wins. Single phase.

### Canonical examples

The Anthropic skills published as of 2026-05 fall into these buckets:

  Reference-format (= single phase):
    pdf, docx, xlsx, pptx, claude-api, brand-guidelines,
    canvas-design, frontend-design, algorithmic-art

  Workflow-format (= 2-4 phases when source explicitly stages):
    doc-coauthoring, skill-creator

When in doubt about a NEW skill not on this list, apply the 3 hard
signals above. Default to single phase if any one of them points that
way.

### Why this matters

Mechanical ``read → process → output`` splits:

  - Manufacture phase boundaries the source never had.
  - Force you to invent inter-phase artifacts the source doesn't
    describe (= prone to wrong field names / shapes / runtime
    KeyError-shaped failures).
  - Spread the source's recipes across phases in ways that hide the
    per-operation recipes from the LLM at dispatch time — each phase
    has its own ~500-token budget, so a per-recipe instruction list
    won't fit if it's chopped across multiple phases.
  - The single-phase form preserves the full recipe library in one
    place where the LLM can scan all options and pick the right one
    based on the user's actual request.

### What "single phase" looks like

```yaml
graph:
  <only_phase>: []
```

The phase's ``input`` is ``user_message``; ``can_finish: true``; the
phase's instructions are the source body itself (lifted / lightly
condensed, recipes preserved verbatim so the LLM can apply them). The
``final_output`` reuses ``user_message`` or a single result artifact.
The skill has 0 or 1 custom artifacts total. Lint passes trivially
because there are no inter-phase artifacts to wire.

### What workflow-style decomposition looks like (= when justified)

```yaml
graph:
  interview: [draft]
  draft: [review]
  review: []
```

With 2 inter-phase artifacts (= the answers from interview, the draft
from draft phase) + 1 final-output artifact. Each phase's input is the
previous phase's output, **declared as a real artifact YAML** per the
coverage rule below.

For each phase, decide:

- `name` — short snake_case (e.g. `analyze`, `draft`, `review`)
- `instructions` — the prose for that phase, lifted/condensed from the source
- `input` — name of an **artifact** (NOT a phase name). For the entry phase
  this is normally `user_message` (stdlib). For every later phase this MUST
  be a custom artifact name you define in Step 3 — typically named after
  the previous phase's output, e.g. `<prev_phase>_result` or a domain noun
  like `draft_text`. The lint will fail if `input` names a phase rather
  than an artifact you've declared.
- `can_finish` — true only on the last phase

**CRITICAL — artifact coverage rule** (= same shape as ``skill_builder``):
Every phase's ``input`` MUST be either ``user_message`` (stdlib) OR an
artifact you write a YAML schema for in Step 4. If you split the source
into N phases, you need at least N-1 inter-phase artifacts plus the
final-output artifact. Counting check:

  - 1-phase skill: 0 custom artifacts (the phase's ``input`` is
    ``user_message``; ``final_output`` can be ``user_message`` too).
  - 2-phase skill: 1 inter-phase artifact + 1 final-output artifact
    (the final-output may reuse the inter-phase one if the second
    phase produces the user-visible result directly).
  - 3-phase skill: 2 inter-phase artifacts + 1 final-output (often
    the last inter-phase IS the final output).

Lint catches missing artifacts; write them all in Step 4.

### Optional: python preprocessor

If the source explicitly describes deterministic computation that the LLM
would otherwise have to perform unreliably — counting, parsing, regex
extraction, format conversion, hashing, statistics — and the source's
intent makes it clear this *should* be precise, consider adding a `python`
preprocessor step to the relevant phase. Examples worth converting:

- "count the words", "split into 3 sections", "extract email addresses"
- "compute the SHA-256", "estimate token count", "parse the ISO timestamp"
- "validate the input is a valid URL"

Stay conservative — Anthropic skills are written for LLM-only execution,
so the original author didn't ask for Python. **Only add a python step
when the source clearly calls for deterministic precision** and the LLM
would otherwise be unreliable. Default to NOT adding python; let the user
run skill_improver later if eval reveals a need.

When you do add python, also write a small `./preprocessing.py` next to
`skill.md` with the function definitions (Step 4 covers the file write).

## Step 3 — Decide skill identity

From the source's frontmatter (or the registry candidate name) derive:

- **Slug** — lowercase snake_case, e.g. `pdf_summarizer`, `code_reviewer`.
  Used as the directory name and the skill `name:` field.
- **Description** — one line, in the user's language if the source had it
  in that language.
- **Final output** — typically a `text` or `result` artifact. If the source
  produces structured data, declare a per-skill artifact for it; otherwise
  reuse `user_message` as a passthrough wrapper.

## Step 4 — Write the files

In one act turn, emit `file` ops with `op: write` for:

### `reyn/local/<slug>/skill.md`

```yaml
---
type: skill
name: <slug>
description: <one-line>
entry: <first_phase_name>
final_output: <output_artifact_name>
final_output_description: <one-line>
finish_criteria:
  - <criterion 1>
  - <criterion 2>
graph:
  <phase_a>: [<phase_b>]
  <phase_b>: []
imported_from: <source_url>
imported_at: <iso_timestamp_utc>
imported_format: anthropic-skills/v1
---

## Overview

<one or two paragraphs lifted from the source>

## Source

This skill was imported by `skill_importer` on <iso_date> from:
<<source_url>>

To re-import the latest version, run:

    reyn run skill_importer "<query>"
```

The `imported_*` keys MUST appear in the frontmatter. They are inert metadata
the parser ignores; they exist so the user can see and the future sync skill
can find the upstream.

If the URL contains a git commit SHA (e.g.
`raw.githubusercontent.com/<owner>/<repo>/<sha>/<path>`), also include
`imported_revision: <sha>`.

### `reyn/local/<slug>/phases/<phase_name>.md` (per phase)

```yaml
---
type: phase
name: <phase_name>
input: <input_artifact_name>     # MUST name an artifact (= user_message or a YAML
                                 # file you write below). NEVER a phase name.
role: <optional_role>
can_finish: <true|false>
allowed_ops: [<op_kinds>]
---

<phase instructions, lifted/condensed from the source>
```

`allowed_ops` lists the Control IR op kinds the phase actually emits. Pick
the smallest set from `op_catalog` — narrower lists keep the LLM on task
and reduce prompt tokens. If the source skill calls tools (HTTP fetch,
shell, file I/O), map them to the closest reyn ops. If the phase emits no
side effects (pure judging/routing), use `[]`. Omit the field to inherit
the default `[file, ask_user]`.

If the phase has a python preprocessor (per Step 2's decision), include
`preprocessor` and `permissions.python` blocks in the frontmatter:

```yaml
---
type: phase
name: <phase_name>
input: <input_artifact_name>
preprocessor:
  - type: python
    module: ./preprocessing.py
    function: <function_name>
    into: data.<field>
    output_schema:
      type: object
      properties:
        <field>: {type: <type>}
      required: [<field>]
permissions:
  python:
    - module: ./preprocessing.py
      function: <function_name>
      mode: safe
---
```

### `reyn/local/<slug>/preprocessing.py` (only if needed)

When you added python preprocessor steps in Step 2, write the function
definitions here. Plain Python file, no frontmatter:

```python
def <function_name>(artifact: dict) -> dict:
    # deterministic computation, stdlib only (pure mode)
    ...
    return {...}
```

Stick to the stdlib (math, statistics, json, re, datetime, hashlib,
collections, etc.) — pure mode rejects other imports. If the source
absolutely needs a 3rd-party library, declare `mode: unsafe` in the
phase frontmatter and add a note in the `## Source` section that the
user must `reyn run --allow-untrusted-python` for this skill to work.
(Note: `unsafe` replaces the old `trusted` keyword as of FP-0014.)

### `reyn/local/<slug>/artifacts/<art>.yaml` (one per non-stdlib artifact)

Write a YAML schema file for **every artifact** any phase declares as
``input`` other than ``user_message``, **and** for the ``final_output``
artifact if it isn't ``user_message``. Skipping this is the single
biggest source of lint failures on import — the OS validates that
every phase's input has a corresponding artifact definition.

Per-file shape (= same format as ``skill_builder`` writes):

```yaml
name: <art>
description: One sentence describing what this artifact contains.
schema:
  type: object
  properties:
    <field_name>:
      type: <string|integer|number|boolean|array|object>
      description: One sentence on what this field represents.
    # ... more fields as needed ...
  required: [<field_name>, ...]
```

Rules:

- ``type: object`` at the top level — never a bare string / array.
- Every property has a ``description``.
- ``required`` lists the fields the consuming phase can rely on (= the
  ones it dereferences in its instructions).
- For an artifact that just carries text between phases, a single
  ``text: { type: string }`` field is fine.
- Match the field names the phase instructions actually reference
  (``input_artifact.data.<field>``) — mismatched names cause the
  consuming phase to see undefined fields at runtime.

Examples for a 3-phase skill `pdf` (= our smoke-test case):

```yaml
# artifacts/extracted_pdf.yaml — what read_pdf produces
name: extracted_pdf
description: Raw text + page count extracted from a PDF source.
schema:
  type: object
  properties:
    text:        { type: string,  description: "Concatenated page text." }
    page_count:  { type: integer, description: "Number of pages." }
    source_path: { type: string,  description: "Original PDF path or URL." }
  required: [text, page_count, source_path]
```

```yaml
# artifacts/processed_pdf.yaml — what process_pdf produces
name: processed_pdf
description: Result of the requested PDF operation (text, file path, or both).
schema:
  type: object
  properties:
    operation: { type: string, description: "Which operation ran (merge/split/extract/etc.)." }
    output_text: { type: string, description: "Text output if applicable (else empty)." }
    output_path: { type: string, description: "File output path if applicable (else empty)." }
  required: [operation]
```

For passthrough skills using only ``user_message`` end-to-end (= rare,
truly single-phase imports), no artifact files are needed (stdlib
provides ``user_message``). Anything with 2+ phases needs at least
one custom artifact.

## Step 5 — Lint the result

In one act turn, emit a `lint` op:

```json
{"kind": "lint", "skill_path": "reyn/local/<slug>"}
```

Capture `passed`, `error_count`, and any issues in the result.

## Step 6 — Decide turn

Emit `skill_import_result` with:

- `installed_path`: `reyn/local/<slug>`
- `skill_name`: `<slug>`
- `source_url`: from `selected_candidate`
- `imported_at`: the same UTC ISO timestamp you wrote into the frontmatter
- `phases`: list of phase names you created (entry first)
- `lint_passed`: result of Step 5
- `notes`: any caveats — fields skipped, ambiguous decomposition, lint
  warnings the user should address, etc.

control.type = "finish".

## Constraints

- **One web_fetch** for the source. Don't fetch other URLs.
- Write only under `reyn/local/<slug>/` — never outside that directory.
- The `imported_at` value MUST be UTC and ISO 8601, e.g.
  `2026-04-30T05:42:18Z`. Use the time you start the convert phase, not
  the original source's date.
- Do NOT include credentials, API keys, or anything else in the
  imported_from URL. If the source URL contained query-string secrets,
  strip them before recording (this should never happen for public
  registries but be defensive).
- If the source is empty, malformed, or clearly not a skill, abort with
  control.type='abort' and a summary explaining what was wrong.
