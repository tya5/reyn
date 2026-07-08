---
type: concept
topic: runtime
audience: [human, agent]
search_hints: [present, presentation, present op, present layer, declarative UI, blueprint, JSON pointer binding, presentation guard, fallback chain, replay as cache, bulk data output, output tokens]
---

# Present layer

The **present layer** lets an agent show bulk data to the user **without the data
passing through LLM output tokens**. An agent that has obtained a large result — a
table of search hits, a file, a structured API payload — routes the data's *handle*
plus a declarative *display template* straight to the user-facing surface. The bulk
bytes reach the user directly; the LLM never re-types them.

## The problem: bulk data costs output tokens

When an agent obtains external data and wants to show it, the data traditionally
round-trips through the LLM twice:

- **Input** — the tool result enters the LLM's context. reyn already solves this: a
  large result lands in a **ref file** and the LLM sees only a schema + preview, reading
  the full value back on demand (see [Workspace](workspace.md) and the offload
  mechanism).
- **Output** — to show the data to the user, the LLM **re-types it as output tokens**.
  This is the axis the present layer solves. Re-typing 500 rows is expensive, and to fit
  its output budget the LLM summarizes or truncates — so the user *also* loses fidelity.

The offloaded ref file is already "data file + handle". What was missing is a primitive
that routes that handle plus a template to the user's surface directly. That primitive is
the **`present` op**.

## LLM sees shape, the user sees content

The defining asymmetry of the present layer:

> The LLM works from the schema + preview and **binds paths**; the renderer joins the
> template against the **full data the LLM never ingested**. The user sees everything;
> the LLM sees only the shape.

This is the designed contract, not a defect. **Display is free; computation costs.**
Presenting N rows costs ~0 output tokens. The moment the agent must *transform* the data
— sort it, filter it, answer a question about it — it must read the ref and pay for the
tokens. Keeping display cheap and transformation costly is the whole point.

Whether the LLM had actually read the data before presenting it is a fact the OS can
compute (was the data inline, or does a prior read of the ref appear in the session?).
The present layer does not forbid **blind** presentation — it makes blindness
**auditable**, recording it as an annotation on the audit event.

## The declarative model — never executable

A template is a **declarative component tree**, never code. It is built from a fixed
**catalog** of read-only components:

| Component | Shows |
|---|---|
| `text` / `markdown` | a string (markdown is rendered as CommonMark) |
| `code` | source with a language for highlighting |
| `diff` | a unified diff |
| `keyvalue` | a card of label/value rows |
| `table` | rows × columns |
| `list` | a bullet list |
| `image` | routes to the multimodal delivery path |

Data is joined to the template by **JSON Pointer (RFC 6901)** path bindings — expressed
structurally as `{"$bind": "<pointer>"}`. `table` and `list` paths resolve **row-relative**
(relative to each iterated row). Everything that is not a `$bind` is a literal.

No markup, no HTML, no code ever crosses from the LLM to the renderer. Safety comes from
the primitive's **shape** — a vetted catalog + path bindings — the same "safety by
construction" philosophy as reyn's structural write-gate. **v1 is display-only**: there
are no buttons, forms, or other interactive components. UI-spoofing (fake consent dialogs)
only becomes harmful with interactivity, so reyn structurally avoids the whole class;
interactivity, when it comes, will route through the existing intervention (consent) path.

## Two safety layers: guard vs renderer discipline

Leaf-string safety is split across two layers, because a single "escape everything"
strategy corrupts markup-inert sinks:

- **Presentation-guard (surface-universal, the output seam).** Runs **unconditionally** —
  including, and especially, for never-ingested data — over **every** render leaf (labels,
  literals, and bound values) at one seam. It neutralizes threats that are dangerous to a
  surface *regardless of which component receives the content*. On the terminal that means
  **stripping ESC / control sequences** (OSC / CSI — notably the OSC-52 clipboard escape).
  That is the guard's whole terminal job: it does **not** HTML-escape (a `<div>` is inert
  literal text in a terminal, and escaping would corrupt `code`/`diff`) and does **not**
  escape Rich console markup. A future web surface plugs in HTML/JS escaping as *its* guard
  strategy, without touching the core.
- **Renderer discipline (per-component, render-API safety).** Rich console markup
  (`[red]…[/]`) is not a surface-universal threat — it is interpreted **only** by a specific
  print call, never by an inert sink. So the terminal renderer achieves markup safety
  *structurally*: it routes every untrusted leaf into a markup-inert Rich object and never
  asks Rich to interpret markup on leaf content. Injection becomes impossible with **no
  escaping anywhere**.

Per-binding **size caps** stop a root pointer bound into a `text` component from dumping a
whole file, and `present` carries its **own** default output cap (head-N rows/lines + a
`…N more — full data: <ref>` tail) because it is unbounded by construction. The **ref is
always the full-fidelity escape hatch**: re-present with a filter or a higher cap, or read
it directly.

## Degrade, never fail — the 4-stage fallback

A binding miss never loses the user's access to the data. Template resolution degrades
through four stages, the last of which **always renders**:

1. **Registered template** — a named template from the operator's registry.
2. **Inline blueprint** — an LLM-authored component tree, structurally gated at op
   validation.
3. **Default viewer** — a blueprint synthesized from the data's *shape* (`list[dict]` →
   `table`, `dict` → `keyvalue`, scalar → `text`, a diff-sniff → `diff`), run through the
   same bind → guard → render path.
4. **Generic** — the final catch: structured data dumped as YAML into a `text` component,
   plain text shown as-is.

The fallback fires on an all-miss template or an unknown template name — never a hard
error. The op's **ack** reports the *requested* template's stats plus a `note` naming the
stage that actually rendered, so a blind agent self-corrects for a few tokens: many
`path_not_found` drops read as "my template doesn't match this data shape", `type_mismatch`
as "right path, wrong component", `guard_stripped` as "content neutralized by the guard,
not a template bug".

Named templates are registered by an **operator** in a config file; the LLM only ever
authors inline blueprints. This mirrors reyn's write-gate culture — the durable, reusable
surface is operator-owned.

## Audit-first, and replay-as-cache

Every presentation emits a durable **`presented`** audit event carrying **refs + stats
only, never content bytes** — the data is already durable in the ref file, so the event
stays light. The event records the data ref, the template name (or a hash of the inline
blueprint), the surface, the OS-computed `ingested` annotation, and the binding stats.

A presentation is a **cache**; the `presented` event is the **truth**. When a session is
[replayed](events.md) or rewound, a `presented` event is re-rendered **best-effort**: if
the ref is still readable, its content is re-synthesized from the data's shape (the event
never stored the bytes); if the ref is **gone** (garbage-collected or unavailable) — or the
data was inline and never persisted — the replay shows an **expiry placeholder** pointing
at the durable audit event. It is never a crash and never a stale render. `present` pins
nothing into a retention window: refs keep their existing lifecycle, and because the
conversation history never contains the presented bytes, there is nothing new to compact.

## See also

- [Reference: present op & surface](../../reference/runtime/present.md) — op args, catalog,
  binding, registration, ack, the `presented` event, and the replay note.
- [Control IR](../../reference/runtime/control-ir.md) — the `present` op in the op catalog.
- [Events](events.md) — replay and the audit log.
- [Workspace](workspace.md) — refs and the offload mechanism `present` consumes.
