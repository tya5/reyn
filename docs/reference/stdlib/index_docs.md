---
type: reference
topic: stdlib
audience: [human, agent]
applies_to: [index_docs]
---

# `index_docs`

Build a searchable semantic index over a path glob (ADR-0033 §2.1).

## Entry

`strategy`

## Final output

`index_summary` — chunk count, embed/write stats, and the strategy (`boundary`, `max_chunk_size_tokens`) that was applied.

## How it composes

Two-stage execution. Phase `strategy` (LLM): a preprocessor runs `gather_samples` (up to 5 file excerpts) and `cost_preflight` (chunk count + estimated cost), then the LLM decides the chunking strategy (`boundary`, `max_chunk_size_tokens`, overlap, etc.) or aborts if `threshold_exceeded` is true. Skill.postprocessor (deterministic, no LLM): `extract_and_split` globs files and splits them into chunks, `write_chunks_with_lock` writes `chunks.jsonl` under an advisory source lock, the `embed` op embeds via LiteLLM, and `index_write` persists to `SqliteIndexBackend`.

## Caveats

- Requires `python` (unsafe) permissions for `gather_samples`, `write_chunks_with_lock`, and `apply_strategy`; the `embed` and `index_write` ops require their respective op permissions.
- Concurrent runs against the same source are rejected with `SourceLockedError` (UX gap fix D).
- If `cost.threshold_exceeded` is `true` the LLM must abort regardless of the dollar estimate — the threshold encodes user policy, not cost magnitude.
- The postprocessor does not run when the LLM aborts — no embedding or writing occurs.
- Override `chunkers.py` via `extends: stdlib/index_docs` for project-specific file formats (Python AST, custom Markdown, etc.).

## Usage

Use to make documentation or source code searchable for `recall` queries.

```bash
reyn run index_docs '{"source":"my_docs","path":"docs/**/*.md","description":"My project docs"}'
```

## Source

[`src/reyn/stdlib/skills/index_docs/skill.md`](https://github.com/tya5/reyn/blob/main/src/reyn/stdlib/skills/index_docs/skill.md)
