---
type: reference
topic: cli
audience: [human, agent]
applies_to: [reyn media]
---

# `reyn media`

Inspect Reyn-managed media / tool-result on-disk storage. Read-only — this command
never deletes anything (#4478 Phase 1: measurement only, no TTL/max-N eviction policy
yet; `media_store.py`'s own module docstring names measurement evidence, not
hypothesis, as the precondition for any future Phase 2 eviction).

## Synopsis

```
reyn media stats [--project-root <path>]
```

## Subcommands

### `stats [--project-root <path>]`

Print on-disk file counts + byte totals for `.reyn/media/` and `.reyn/tool-results/`.
Gives `MediaStore.storage_stats()` — previously a measurement method with no caller —
an actual reader, so an operator (or a script) can decide whether disk pressure is real
before any eviction policy gets designed.

```bash
$ reyn media stats

directory            files           bytes
media/                   12         4,718,592
tool-results/            37        18,874,368
```

`--project-root` defaults to the current directory; it must contain a `.reyn/` tree
(the same resolution `reyn chat` and other project-scoped commands use).

## Storage this command reports on

- **`.reyn/media/`** — resolved image/media bytes fetched for the `present` op's
  `image` component (see [Present op reference](../runtime/present.md#v1-catalog-display-only-non-executable)).
- **`.reyn/tool-results/`** — offloaded large tool results (the chat-string offload
  path, `reyn.runtime.services.tool_result_cap.cap_tool_result`).

Both live under `.reyn/cache/` in spirit — fully regenerable, never recovery-core (see
[`.reyn/` directory layout](../runtime/reyn-dir-layout.md) for the five-way subtree
classification) — so nothing this command reports is durable state an operator needs
to back up.

## Spill-manifest self-prune (#4478)

`MediaStore` tracks which tool-result files it has spilled to disk via a manifest at
`.reyn/cache/tool_result_spills.jsonl`, read in full on every `MediaStore`
construction. An entry whose target file no longer exists on disk (deleted manually,
or by a future Phase 2 GC policy) is dropped from the manifest the next time it's
loaded — this bounds the manifest's otherwise-unbounded growth. The prune only
rewrites the manifest itself; it never deletes any actual media/tool-result bytes,
and a write failure here is best-effort (never fails `MediaStore` construction).
`reyn media stats` does not report on the manifest directly — it measures the
artifact directories the manifest tracks.

## Related

- [`.reyn/` directory layout](../runtime/reyn-dir-layout.md) — the five-way subtree
  classification (`.reyn/media/` and `.reyn/tool-results/` are cache, not recovery-core)
- [Present op reference](../runtime/present.md) — the `image` component that
  populates `.reyn/media/`
