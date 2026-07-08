---
type: reference
topic: runtime
audience: [human, agent]
search_hints: [present op, present reference, presentation, data_ref, data_inline, blueprint, template, catalog component, table, keyvalue, list, code, diff, markdown, image, $bind, JSON pointer, presentations.yaml, presentations.entries, present ack, bindings_dropped, presented event, replay, recovery gate, expiry placeholder]
---

# Present op & surface reference

Operator/agent-facing reference for the **present layer** — the `present` op's args, the
v1 component catalog, path binding, named-template registration, the op ack, the
`presented` audit event, and the replay/rewind behavior. For the *why* (the axis-B/C
problem, the LLM-sees-shape/user-sees-content asymmetry, the guard/renderer split), see
[Concepts: Present layer](../../concepts/runtime/present.md). The op also appears in the
[Control IR](control-ir.md#present) op catalog.

## The `present` op

```json
{
  "kind": "present",
  "data_ref": ".reyn/cache/tool-results/2026-.../structured.json",
  "blueprint": {
    "component": "table",
    "rows": {"$bind": "/results"},
    "columns": [
      {"header": "Title",  "path": "/title"},
      {"header": "Author", "path": "/author"}
    ]
  }
}
```

Exactly one **data source** and exactly one **template**:

| Arg | Type | Notes |
|---|---|---|
| `data_ref` | string | **XOR** `data_inline`. Any zone-readable path. An offloaded `structured_ref` is **re-hydrated to its full value** (not read from the LLM-visible preview) via `file.read` semantics. |
| `data_inline` | any | **XOR** `data_ref`. Small data already in the LLM's context (convenience). |
| `template` | string | **XOR** `blueprint`. A registered presentation name (see registration below). An unknown name is not an error — it falls through the fallback chain. |
| `blueprint` | object \| array | **XOR** `template`. An inline declarative component tree (a single node or a top-to-bottom list). |

- **Tier 0** (`ask_user`'s sibling), **fire-and-continue** — presenting to the user (the
  trust root) has no output permission gate, and unlike `ask_user` it does **not** pause
  the run. The one gate: `data_ref` read authority resolves **identically to `file.read`**
  — `present` can never read more than the agent's file ops can (`present` denied ⇔
  `file.read` denied).

## v1 catalog (display-only, non-executable)

All components are read-only. A blueprint node is `{"component": <name>, ...slots}`.

| Component | Slots |
|---|---|
| `text` | `text` (bind or literal) |
| `markdown` | `text` — rendered as CommonMark |
| `code` | `text`, `language?` |
| `diff` | `text` — unified diff |
| `keyvalue` | `rows: [{label, value}]` |
| `table` | `rows` (bind → array), `columns: [{header, path}]` |
| `list` | `items` (bind → array), `item_path?` (per-item path) |
| `image` | `src`, `alt?` — v1 renders an `[image: <alt>]` dim-text placeholder only, not yet routed to the multimodal delivery path |

There are **no interactive components** (no buttons / forms) in v1.

### Binding — `$bind` / JSON Pointer

Data is joined to a template by **JSON Pointer (RFC 6901)** paths, expressed structurally:

- `{"$bind": "/results/0/title"}` — a pointer string; `""` binds the **whole document**.
- Anything that is not a `$bind` object is a **literal** (e.g. a `header` string).
- `table` `columns[].path` and `list` `item_path` resolve **row-relative** (relative to
  each iterated row).

Binding outcomes (§4): path hit → bind; path miss → **soft-skip** + record
`path_not_found`; type mismatch → coerce (a scalar into a `table` `rows` slot → a 1-row
table) + record `type_mismatch`; a leaf neutralized/size-capped by the guard → record
`guard_stripped`. When **all** bindings miss, the op reports `all_bindings_missed` and
routes to the fallback chain — never a hard failure.

The structural gate at op validation rejects a **non-catalog component** or a **non-path
binding** as a hard error (`status="error"`) for an inline blueprint — that is a template
bug, distinct from a soft binding drop.

## Named-template registration (operator-only)

Named templates are registered in **`presentations.yaml`** (`presentations.entries`) — an
**operator/config action**. There is no install op; the LLM authors inline blueprints only.

```yaml
presentations:
  entries:
    search_results:
      blueprint:                              # required; inline component tree
        - component: table
          rows: {"$bind": "/results"}
          columns:
            - {header: Author, path: /author}
            - {header: Title,  path: /title}
      description: "Search results table"      # optional
      enabled: true                            # optional, default true
```

The blueprint is validated at load; the `<project>/.reyn/config/presentations.yaml` layer
hot-reloads at the turn boundary. Full field table + merge order:
[reyn.yaml § presentations](../config/reyn-yaml.md#presentations-block).

## Template fallback — 4 stages

Resolution degrades until something renders (never a hard error):

1. **Registered `template`** → 2. **inline `blueprint`** → 3. **default viewer**
(synthesized from data shape: `list[dict]` → `table`, `dict` → `keyvalue`, scalar →
`text`, diff-sniff → `diff`) → 4. **generic** (structured → YAML into `text`, plain text
as-is — always renders).

The fallback fires on an all-miss template or an unknown template name. The ack reports the
**requested** template's stats plus a `note` naming the stage that actually rendered.

## Ack (op result)

The LLM's only feedback — compact + high-signal:

```yaml
ok: true
bindings_resolved: 3
rows: 500
bindings_dropped:
  - {path: "/results/0/author", reason: path_not_found}
  # reason ∈ {path_not_found, type_mismatch, guard_stripped}
all_bindings_missed: false
note: "…"        # present only when a fallback stage rendered
```

`path_not_found` across many rows → "template doesn't match this data shape";
`type_mismatch` → "right path, wrong component"; `guard_stripped` → "content neutralized by
the guard, not a template bug". The agent self-corrects without ingesting the data.

## `presented` event (P6 audit)

Every presentation emits one `presented` event carrying **refs + stats only, never content
bytes**:

| Field | Meaning |
|---|---|
| `data_ref` | the ref path, or `<inline-data>` for a `data_inline` presentation |
| `template` | the registered name, or `blueprint:<hash>` for an inline blueprint (no blueprint bytes) |
| `surface` | list, e.g. `["inline-cui"]` (`["null"]` when no renderer is wired) |
| `ingested` | `none` \| `partial` \| `full` — **OS-computed** (was the data inline, or does a prior `read_file` on the ref appear earlier in the session?), never LLM-self-reported |
| `bindings_resolved` | count of resolved bindings |
| `bindings_dropped` | `[{path, reason}]` |
| `rows` | row count bound |

## Replay / rewind — presentation as cache

A presentation is a **cache**; the `presented` event is the **truth**. On replay
(`reyn events <log>`) or rewind, a `presented` event re-renders **best-effort**:

- **Ref still readable** → the content is re-synthesized from the data's shape (the event
  never stored the bytes, so this uses the default/generic viewer, not the caller's
  original inline blueprint) and reaches the surface.
- **Ref gone** (GC'd / unavailable), or the data was **inline** and never persisted → an
  **expiry placeholder** pointing at the durable `presented` audit event. Never a crash,
  never a stale render.

`present` pins nothing into a retention window; refs keep their existing lifecycle, and the
conversation history never contains the presented bytes (nothing new for compaction).

### Recovery-feature gate — not applicable

The CLAUDE.md **recovery-feature truncate-falsify gate** (a PR adding WAL-event-derived
reconstruction / PITR / rewind-restore state must prove the reconstruction source survives
WAL truncation) **does not apply** to the present layer. Replay here reconstructs **no
authoritative state**: it produces a **display-only projection** — a best-effort re-render
of an already-durable ref, or a placeholder — and `present` writes **no recovery-core
state**. Nothing derives recoverable state from `presented` events. Were a future revision
ever to reconstruct authoritative state from `presented` events, that PR would have to
carry the truncate-falsify test in-arc.

## See also

- [Concepts: Present layer](../../concepts/runtime/present.md)
- [Control IR](control-ir.md#present) — the op in the catalog
- [reyn.yaml § presentations](../config/reyn-yaml.md#presentations-block) — registration
- [Events](../../concepts/runtime/events.md) — replay and the audit log
