---
type: reference
topic: cli
audience: [human, agent]
applies_to: [reyn storage]
---

# `reyn storage`

Inspect Reyn-managed on-disk storage: media, offloaded tool results, and every
agent's `history.jsonl`. Read-only — **this command itself** never deletes anything
(#4478/#4476 Phase 1: measurement only). `media_store.py`'s own module docstring
named measurement evidence, not hypothesis, as the precondition for eviction — that
precondition has since been met for offloaded tool results specifically: `.reyn/media/`
still has no eviction of any kind, but `.reyn/memory/history-content/` now has both a
per-session byte cap that evicts on every write (#5364 §1.6/#5388) and a project-wide
`storage.max_bytes`/`storage.pin` config with its candidate order computed (#5366
1/N–2/N) — no delete pass wired to the project-wide cap yet. `reyn storage` itself
stays read-only regardless: it never triggers any of the above, only measures it.

Named `storage`, not `media` — the command started as `reyn media stats` (#4478),
then #4476 landed `history.jsonl` reporting on the same surface rather than as a
second command (one operator-facing place to look, since both answer the same
underlying question — "how much of reyn's own on-disk footprint currently
exists" — for different subsystems). "media" stopped describing what the command
covers once `history.jsonl` joined it, so the command was renamed (#4488) before its
reference page ever landed.

## Synopsis

```
reyn storage stats [--project-root <path>]
```

## Subcommands

### `stats [--project-root <path>]`

Print on-disk file counts + byte totals for `.reyn/media/` and
`.reyn/memory/history-content/` (#5364: the tool-result write location moved here;
see [Storage this command reports on](#storage-this-command-reports-on) below),
plus file/byte/turn counts summed across every `history.jsonl` found under
`.reyn/agents/`. Gives `MediaStore.storage_stats()` and `aggregate_history_stats()` —
each previously a measurement method with no caller — an actual reader, so an
operator (or a script) can decide whether disk pressure is real before any
eviction/retention policy gets designed.

```bash
$ reyn storage stats

directory                      files           bytes
media/                            12       4,718,592
memory/history-content/           37      18,874,368

                     files           bytes       turns
history.jsonl             3         623,411         842
```

`--project-root` defaults to the current directory; it must contain a `.reyn/` tree
(the same resolution `reyn chat` and other project-scoped commands use). A project
with no `.reyn/agents/` yet reports all-zero for the `history.jsonl` row, not an error.

## Storage this command reports on

- **`.reyn/media/`** — resolved image/media bytes fetched for the `present` op's
  `image` component (see [Present op reference](../runtime/present.md#v1-catalog-display-only-non-executable)).
- **`.reyn/memory/history-content/`** — offloaded large tool results (the
  chat-string offload path, `MediaStore.save_tool_result`), CURRENT writes
  only, nested two levels — agent, then session (#5364; #5383's own
  key-space fix: session id alone collided every agent's default `main`
  session into one shared directory). Per
  [`.reyn/` directory layout](../runtime/reyn-dir-layout.md) this location is
  classified **persist**, not `cache/` — the bytes it accumulates are not
  something a future eviction policy simply rebuilds from elsewhere.
- **`.reyn/tool-results/`** (pre-#5364, frozen — no longer written, no
  migration) — the OLD offload location; still resolvable by an
  already-minted path-ref, but **not** counted by `reyn storage stats`
  (`storage_stats()` measures the current write location above). Per
  [`.reyn/` directory layout](../runtime/reyn-dir-layout.md), `media/` and
  `tool-results/` are classified **audit** — an append-only record kept,
  never restored on rewind — not `cache/`, despite being safe to grow
  without affecting recovery correctness. An operator upgrading from a
  pre-#5364 checkout may still be carrying bytes under `tool-results/`
  that this command no longer reports on; nothing deletes them.
- **`history.jsonl`** (one per agent/session, `.reyn/agents/<name>/history.jsonl` and
  `.reyn/agents/<name>/sessions/<sid>/history.jsonl`, glob `**/history.jsonl`) — the
  durable, append-only turn log `CompactionController` and branch-visibility filtering
  read directly (#4472); `total_lines` counts non-empty JSONL lines the same way a
  real reader does, not a raw line count. This command counts and sizes these files;
  it does not itself appear in
  [`.reyn/` directory layout](../runtime/reyn-dir-layout.md)'s canonical tree today —
  that page's five-way classification does not yet have a `history.jsonl` row (a
  pre-existing doc gap this command's landing surfaced, not something #4476/#4488
  changed).

## Spill-manifest self-prune (#4478)

`MediaStore` tracks which history-content files it has spilled to disk (any turn's
content, not just a tool result — #5564: the read-side check,
`MediaStore.is_history_content_spill`, never branched on the spilling turn's
role) via a manifest at `.reyn/memory/tool_result_spills.jsonl` (#4584: moved
from `.reyn/cache/` — that
tier's "derived, rebuilt after restore" promise never held for this manifest; see
[`.reyn/` directory layout](../runtime/reyn-dir-layout.md)), read in full on every
`MediaStore` construction. An entry whose target file no longer exists on disk
(deleted manually, or by the per-session history-content eviction — #5364 §1.6/#5388,
live for offloaded tool results) is dropped from the manifest
the next time it's loaded — this bounds the manifest's otherwise-unbounded growth.
This is a self-PRUNE of existing entries only, never a REBUILD: if the manifest file
itself were deleted, nothing recreates it. The prune only rewrites the manifest
itself; it never deletes any actual media/tool-result bytes, and a write failure
here is best-effort (never fails `MediaStore` construction).
`reyn storage stats` does not report on the manifest directly — it measures the
artifact directories the manifest tracks.

## Related

- [`.reyn/` directory layout](../runtime/reyn-dir-layout.md) — the five-way subtree
  classification (`media/`/`tool-results/` are audit; `memory/history-content/`
  is persist, #5364; `history.jsonl` is currently undocumented there)
- [Present op reference](../runtime/present.md) — the `image` component that
  populates `.reyn/media/`
