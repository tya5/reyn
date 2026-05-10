---
type: phase
name: strategy
input: index_docs_input
role: index_strategist
can_finish: true
allowed_ops: []
preprocessor:
  - type: python
    module: ./chunkers.py
    function: gather_samples
    into: data.samples_result
    output_schema:
      type: object
      required: [samples, summary, file_count]
      properties:
        samples:
          type: array
          description: Up to 5 representative file excerpts for strategy decision.
          items:
            type: object
            required: [path, excerpt, size_tokens, structure_hint]
            properties:
              path:           {type: string}
              excerpt:        {type: string}
              size_tokens:    {type: integer, minimum: 0}
              structure_hint: {type: string}
        summary:
          type: object
          required: [file_count, total_bytes]
          properties:
            file_count:  {type: integer, minimum: 0}
            ext_dist:    {type: object}
            total_bytes: {type: integer, minimum: 0}
            mean_bytes:  {type: integer, minimum: 0}
        file_count:
          type: integer
          minimum: 0
          description: Total number of files matched by the path glob.
  - type: python
    module: ./chunkers.py
    function: cost_preflight
    into: data.cost
    output_schema:
      type: object
      required: [chunk_count, estimated_tokens, estimated_cost_usd, threshold_exceeded]
      properties:
        chunk_count:         {type: integer, minimum: 0}
        estimated_tokens:    {type: integer, minimum: 0}
        estimated_cost_usd:  {type: number,  minimum: 0.0}
        model:               {type: string}
        threshold_exceeded:  {type: boolean}
        error:               {type: string}
---

Decide a chunking strategy for the source being indexed. You have already
received file samples and a cost estimate from the OS preprocessor — use
them, do not compute your own.

## Inputs

- **Source**: `data.source` — `data.description`
- **Path glob**: `data.path`
- **File summary**: `data.samples_result.summary`
  - `file_count` — number of files matched
  - `ext_dist` — extension breakdown (e.g. `{".md": 45, ".py": 12}`)
  - `total_bytes` / `mean_bytes` — size profile
- **File samples**: `data.samples_result.samples`
  - Up to 5 representative excerpts with `structure_hint` (e.g. "Markdown with headings")
- **Cost preflight**: `data.cost`
  - `chunk_count` — estimated total chunks
  - `estimated_cost_usd` — embedding cost at default model rates
  - `threshold_exceeded` — True if chunk_count exceeds `cost_warn_threshold`

## Decision: Choose `chunk_strategy`

Based on the file samples and structure, decide:

- `boundary`: where to split chunks
  - `heading`: Markdown `#` / `##` headings — best for structured docs with sections
  - `blank_line`: paragraph / code block boundaries (double-newline) — best for prose, scripts
  - `sentence`: sentence-level — best for QA retrieval over dense prose
- `max_chunk_size_tokens`: 100–4000 tokens
  - Smaller (200–400): QA-style retrieval, short answers, FAQ
  - Larger (600–1000): context-heavy docs, code, long-form technical content
- `min_chunk_size_tokens` (optional, default 50): merge tiny fragments
- `overlap_ratio` (optional, default 0.0): prose benefits from 0.10–0.20; code 0.0
- `preserve_parent_context` (optional, default true): emit heading/class/function name as metadata

**Passthrough**: echo `data.source`, `data.path`, `data.description`, and
`data.mode` (or `"append"` if absent) back in the artifact — the postprocessor
pipeline reads them from the finish artifact.

## Cost gate (UX gap fix B)

If `data.cost.threshold_exceeded` is true, OR if `data.cost.estimated_cost_usd`
is unexpectedly high for the number of files (e.g. > $1.00 for a small path),
emit `decision: "abort"` with a clear explanation in `control.reason.summary`.
The Skill.postprocessor will not run when the LLM aborts — no chunks will be
embedded or written.

Otherwise emit `decision: "finish"` with the `chunk_strategy` artifact.

## Constraints

- Do NOT emit any ops. Emit only a decide turn.
- Fill all required fields of `chunk_strategy`.
- Echo `source`, `path`, `description`, `mode` verbatim from the input.
- Trust the preprocessor data — do not re-estimate file counts or costs yourself.
