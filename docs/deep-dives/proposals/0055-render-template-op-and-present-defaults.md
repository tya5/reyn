# render_template op + present defaults — text templating without bloating the present layer

**Author:** architect · **Status:** IMPLEMENTED 2026-07-09. Arc landed: PR-0 #2674
(sandbox env extraction → `security/template_env.py`), PR-1 #2676 (present `view`
rename + optional view + #2670 fail-closed), PR-2 #2678 (`render_template` op + its own
canonical mapper). Follow-up: #2679 (operator-yaml bounds config). · **Date:**
2026-07-09 · **Builds on:** [FP-0054 present layer](0054-present-layer.md) (IMPLEMENTED)

## Requirements (owner)

1. **One-shot present (approved):** `present(data_ref="<ref>")` with no view/blueprint
   should "just show it" — route straight to the landed stage-3/4 default-viewer
   synthesis.
2. **Text templating (new):** structured data (file/inline) + a text template
   (file/inline) → render → show/use the resulting string. **Engine locked by owner:
   Jinja2, reusing `jinja2.sandbox.SandboxedEnvironment`** (already used by
   `src/reyn/hooks/render.py`, #1800 slice B) — no-arbitrary-code, no engine
   reinvention.

## Design conclusion (summary)

- **Do NOT extend present with a text-template mode.** Ship a **standalone
  `render_template` op**: a generic, sandboxed **producer** (`data + template →
  string`) whose output flows to any **sink** — `present`, `file` write, messages, or a
  pipeline `ctx`.
- **present changes only minimally** (requirement 1): `view`/`blueprint` become
  optional (arg renamed from `template` per the naming partition below); omission
  enters the existing fallback chain at stage 3.
- **Declarative present remains the default and recommended path** for showing
  structured data (token-economical, standards-aligned, portable). `render_template` is
  the escape hatch for **computed text** — loops/conditionals/aggregation woven into
  prose — which declarative binding intentionally cannot express.

## Why a standalone op, not a present mode

The exploration (owner + architect, 2026-07-08/09) converged through four observations:

1. **Standards alignment.** A2UI — the declarative agent-UI standard the present layer
   is deliberately isomorphic to — supports *"used as a template and hydrated with
   values"* as its token-saving mechanism and **intentionally excludes string/logic
   templates**: declarative hydration is what keeps "the UI expands it" portable across
   surfaces. Logic templating is *computation*, which belongs core-side; its output is a
   plain string every surface can display trivially. Folding a second, non-declarative
   paradigm into present would blur exactly the boundary the standards keep sharp.
2. **Token economy does not require embedding.** The original argument for a built-in
   mode was "a standalone op returns the rendered bulk through LLM tokens." Two landed
   mechanisms void it: (a) **pipeline `ctx`** passes step outputs executor-side without
   entering LLM context (0053 §3 — pipeline internals never offload, full values), so
   `render_template → present` as an inline pipeline is zero-token for the bulk; (b) on
   the direct chat path, the **tool-result offload** caps the returned string to a
   preview + `ref` automatically, and `present(data_ref=<that ref>)` shows it — bulk
   stays out of context there too.
3. **Sinks beyond present.** Rendered text is equally useful written to a file
   (reports, config, scaffolding, message bodies). That makes `render_template` a
   general **producer**, not a presentation feature — and it forces the layering rule
   in § "Producer neutrality" below.
4. **Single responsibility / security containment.** present stays one paradigm
   (declarative, structurally safe); the sandbox complexity lives entirely in the one op
   that needs it.

## Naming partition (owner-directed: separate cleanly before implementation)

"Template" currently means two unrelated things. Instead of doc-level qualification,
**partition the vocabulary** so each word has exactly one sense:

| Concept | Name | Where |
|---|---|---|
| Declarative, **registered** UI description | **`view`** | `present(view="sales-summary")` |
| Declarative, **inline** UI description | **`blueprint`** (unchanged) | `present(blueprint=...)` |
| Declarative rendering machinery | viewer (existing usage: stage-3 "default viewer") | — |
| **Text template (Jinja2), exclusively** | **`template`** | `render_template(template_ref=...)`; hooks push / pipeline-input templates |
| Registry | `presentations.yaml` (unchanged; entries are named views) | — |

- Rationale: "template" is the universal Jinja2 domain word ("render a template");
  "view" is the universal declarative-presentation word (MVC). The hooks system's
  Jinja2 usage already matches the partitioned sense, and view/viewer pair naturally
  (the description vs the machinery that renders it).
- **Rename scope (clean break, no alias):** present op arg `template` → `view`; ack /
  `presented`-event field `template` → `view` (semantics unchanged: registered name |
  blueprint hash | null); reference/concept docs + tests updated in the same PR
  (#1983 keeps control-ir.md in sync; design records 0054 etc. are historical and stay
  as written). present landed yesterday with zero external consumers — this is the
  cheapest moment the rename will ever have; deferring it makes the two-sense
  overload permanent.
- With the partition, the new op keeps the name **`render_template`** — now
  unambiguous. (Rejected alternative: leave present's arg as-is and name the op
  `render_text_template` — no landed-code touch, but the overload becomes permanent.)

## Part 1 — present: `view` naming + optional view/blueprint (requirement 1)

- **Naming partition applied**: op arg `template` → **`view`**; ack / event field
  `template` → **`view`** (semantics unchanged: registered name | blueprint hash |
  null). Clean break — the old arg name is rejected, no alias.
- `view` / `blueprint` become **optional**; when neither is given, production enters
  the landed fallback chain **at stage 3** (default synthesis from the resolved data —
  PR-C #2664) with stage 4 as the usual final catch. No new machinery.
- **Ack**: unchanged shape; gains `mode: view | blueprint | default`, and with
  `default` the stats are those of the synthesized default view. `note` appears only if
  stage 3 degraded further to stage 4 (there is no requested view to fall back *from*).
- **Event**: `view: null` + the same `mode` discriminator; shape otherwise unchanged
  (PR-A contract).
- Tests (Tier 1): `present(data_ref)` with no view → stage-3 synthesis; ack
  `mode: "default"`; event `view` null; renamed arg round-trips and the old `template`
  arg is rejected (clean break); existing named/inline paths unaffected.

## Part 2 — `render_template` op (requirement 2)

### Contract

```yaml
render_template:
  template_ref: <path>       # XOR template — Jinja2 source
  template: <string>         # inline Jinja2 source (as implemented; control-ir.md matches)
  data_ref: <path>           # XOR data_inline — context data (same resolution
  data_inline: <obj>         #   seam as present: resolve_present_source / file.read)
  undefined: strict | lenient   # default: strict
```

- Returns the rendered **string** as an ordinary op result (canonical `text`). The
  standard tool-result offload applies on the chat path (large output → ref + preview).
- **Its own canonical mapper is part of this op's contract (FP-0056 discipline).**
  `render_template` is a new producer kind; a new op-kind without a `_MAPPERS` entry
  falls to the whole-dict-structured fallback (the exact FP-0056 bug) for **its own**
  result. So PR-2 must register a `render_template` mapper (rendered string → `text`) in
  the same PR — not rely on the fallback. Under FP-0056 PR-F1's registry-derived gate
  this becomes mechanically enforced; until then it is an explicit PR-2 deliverable.
  (Sequencing consequence: PR-2 lands after FP-0056 PR-H, since both edit
  `canonical.py::_MAPPERS`.)
- Template context binds the resolved data under **`data`**
  (`{{ data.results[0].title }}`) — unambiguous, mirrors pipeline `ctx` style.
- **No `as`/format arg** — the op produces a string; what component displays it is the
  sink's (present's) decision.
- **No side effects**: it writes nothing. File output is composition with the existing
  gated `file` write op; display is composition with `present`.

### Engine and trust model

- `jinja2.sandbox.SandboxedEnvironment` — **load-bearing**, because templates may be
  LLM-authored (`template`) and LLM-authored Jinja2 without a sandbox is
  arbitrary-code execution (SSTI). Operator-authored template files get the same
  sandbox as defense-in-depth (no cost, no mode split, no registry needed — template
  files are just zone-readable files).
- **Shared env helper — pre-refactor (owner-directed, lands FIRST as PR-0):** extract
  `make_sandboxed_env(undefined=...)` from `hooks/render.py`'s private
  `_make_env_strict` / `_make_env_silent` into **`src/reyn/security/template_env.py`**
  — the security package is the natural home because the env factory *is* the
  template-execution-safety policy seam (sandbox invariant; undefined policy is the
  caller's knob), beside the other content-boundary transforms (`content_fence`,
  `threat_patterns`, `secret_redaction`). Strictly behavior-preserving: hooks consume
  the helper, hook rendering behavior and its tests unchanged; **no bounds added here**
  (hook templates are operator-trusted; render bounds are op-scope, PR-2). Doing this
  as its own tiny PR keeps the op PR reviewable as pure feature and proves the
  extraction is inert before anything builds on it.
- **Web-templating analogy** (for reviewers): classic web stacks trust the template and
  escape the *data*; here the template itself may be untrusted, hence the sandbox — and
  the "escape the data for the sink" half maps to sink-side neutralization below.
- Jinja2 **autoescape stays OFF**: HTML-escaping in the producer would corrupt file and
  terminal output (`<`/`>`/`&`) — the same category error Option B removed from the
  guard. HTML safety is the future web surface's sink-side concern.

### Producer neutrality (layering rule — mirrors Option B)

`render_template` output is **raw, un-neutralized text**. Neutralization is the
**sink's** responsibility, because sinks disagree about what is dangerous:

| Sink | Neutralization |
|---|---|
| `present` (terminal) | control/ESC strip via the landed guard seam (Option B) |
| `file` write | **none** — a file is inert bytes; stripping/escaping would corrupt the artifact (the exact reason the producer must not neutralize) |
| future web surface | HTML/JS escaping in that renderer |

Layering rule, generalized from the arc: **sandbox = producer-side (template execution
safety, sink-independent); neutralize = sink-side (output-byte safety,
sink-dependent).** Falsify test: render data containing an ESC sequence → the op result
retains the raw bytes; presenting that result strips them at the guard.

#### Structural sink-neutralization contract (required — no convention, no fail-open)

Because the producer is deliberately neutral, safety now **depends on every
live-interpreting sink actually neutralizing** — and "depends on each sink remembering
to" is precisely the failure class of #2670 (a `get_neutralizer` default that silently
failed open). This design forbids that by contract, not habit:

- **Invariant:** no un-neutralized producer output reaches a **live-interpreting
  surface**. A live-interpreting sink is any path that renders bytes to a surface that
  acts on control/markup (terminal, web) — as opposed to an **inert** sink (`file`
  bytes, pipeline `ctx` data, the LLM tool-result channel) where raw is correct.
- **Structural enforcement (not a convention):** every live-interpreting sink routes
  **all** externally-derived content through its own single neutralizer seam with **no
  bypass path**, and a missing/unknown neutralizer must **fail closed**, never fall
  through to raw (the direct #2670 lesson). `present` already satisfies this via the
  Option-B single guard seam; the design must guarantee the same for any other live
  path.
- **The specific path to verify (flagged by lead):** the **message / chat display
  path** — if a `render_template` result is placed into an agent message that prints to
  the terminal, that print path must neutralize it too. Producer output is
  *untrusted-data-derived* and must **not** be treated as trusted agent prose. Either
  the message-print path passes it through the same neutralizer, or producer output may
  only reach a live surface via a neutralizing sink (`present`). No third, bypassing
  path may exist.
- **Test (Tier 1/2):** an ESC/control sequence interpolated by a template is neutralized
  on **every** live-surface path (present *and* message-print), and an unknown-surface
  neutralizer lookup fails closed rather than emitting raw (falsify against a fail-open
  default, mirroring #2670's regression guard).

### Undefined policy — strict by default

- **`strict` (default)**: `StrictUndefined`; any undefined variable → **hard error
  naming the missing variables**. Rationale: with file generation as a first-class
  sink, lenient-silent interpolation quietly writes broken artifacts (a config with a
  blank where a value belonged) — a "hide the bug in the output" failure, the class the
  team pinned against. Loud-by-default; the error names the vars so the LLM
  self-corrects in one turn.
- **`lenient` (opt-in)**: silent `Undefined` for optional-field templating; rendered
  result carries `undefined_vars: [...]` in the op result meta (same high-signal
  self-correction channel as present's `bindings_dropped`).
- Precedent: `hooks/render.py` already chooses per-context (message = strict, bools =
  silent); this op makes the choice an explicit caller knob with the safe default.

### Failure and resource bounds

- Template **syntax error / sandbox violation** → hard error, `Error (template_error):
  <message>` — never a silent fallback (malformed input must not be masked; PR-C
  malformed-blueprint precedent).
- **Output bound (required — new work):** `hooks/render.py` has **no** size/time bounds
  today (its templates are operator-trusted one-liners; verified — no such code). The
  `SandboxedEnvironment` stops SSTI but **not resource exhaustion** — a bounded template
  like `{% for i in range(10**9) %}` still spins/floods. **The cap must be
  *during*-generate, not post-render:** `template.render()` materializes the full string
  first (exhaustion happens before any cap can fire), so use `template.generate(context)`
  (Jinja2's streaming generator), accumulate chunks against a max-chars budget, and
  truncate + stop the moment it is exceeded → hard error naming the cap. Jinja2 does not
  expose an iteration count, so wrap the `generate()` loop in a **wall-clock backstop**
  (break on exceed) — byte-cap + wall-clock together bound it in practice. Config
  defaults in the `safety` spirit, operator-tunable. (Confirmed with lead-coder.)
- **Determinism/replay**: pure function of (template, data) — no clock/random in scope;
  ordinary `CommittedStep` memo replay applies. No new event type; standard op events.
  No reconstructed state → recovery-feature gate N/A.

### Authority

- `template_ref` / `data_ref` resolution ≡ **`file.read`** — the same read-authority
  equivalence and resolution seam present uses (`resolve_present_source`). Inline-only
  invocation is pure computation (no additional gate). No write authority (no side
  effects).
- New op kind ⇒ **`OP_KIND_MODEL_MAP` + `docs/reference/runtime/control-ir.md` section
  in the same PR** (hard rule #1983).

### Composition recipes (documented with the op)

- **Zero-token path (preferred for bulk):** inline pipeline —
  `render_template(data_ref, template_ref)` → `present(ctx.rendered)` (or a `file`
  write step). Bulk stays executor-side.
- **Chat path:** direct call → result auto-offloads past the cap → LLM sees preview +
  ref → `present(data_ref=<ref>)`.
- **Markdown display note:** stage-3 synthesis deliberately defaults `str → text`
  (fidelity ruling), so presenting a rendered-markdown ref *by default* shows the raw
  source. To render it as markdown, pass a one-line blueprint
  (`{component: markdown}` whole-body binding). When #2663 (content_type sidecar) lands,
  a declared type from `render_template` can make this automatic — noted there as a
  consumer.
- **Steering (tool description, not SP):** `render_template`'s op description must say
  "to show structured data to the user, prefer `present` (declarative); use this op only
  when you need computed text — loops/conditionals/aggregation woven into prose." Keeps
  the declarative path the default reach.

## What this deliberately does not do

- **No present text-template mode** — present stays single-paradigm.
- **No text-template registry** — template files are ordinary zone-readable files;
  presentations.yaml remains declarative-only.
- **No producer-side escaping/autoescape** — sink-side per-surface concern.
- **No operator-trust sandbox relaxation** — one sandboxed path regardless of template
  source (simpler; defense-in-depth).
- **No portability claim** — logic templating is core-side computation by design; the
  declarative path remains the portable / A2UI-adjacent one.

## Suggested PR sequencing (post owner-GO)

- **PR-0 (pre-refactor, owner-directed)** — extract `make_sandboxed_env(undefined=...)`
  into `src/reyn/security/template_env.py`; `hooks/render.py` consumes it. Strictly
  behavior-preserving, no new functionality, no bounds. Lands first and alone so the
  extraction is proven inert before anything builds on it.
- **PR-1** — present naming partition + optional view: arg/ack/event `template` →
  `view` (clean break, one schema touch) + entry-at-stage-3 on omission + ack
  `mode: view|blueprint|default` + event `view: null` + reference/concept doc updates
  (#1983 sync for the changed op schema) + tests.
- **PR-2** — `render_template` op: contract + strict/lenient undefined + streaming
  output cap + wall-clock backstop + producer-neutrality falsify test +
  `OP_KIND_MODEL_MAP` / control-ir.md sync (#1983). Builds on PR-0's helper.

PR-0 and PR-1 are independent of each other; PR-2 depends on PR-0 only.

## Test plan (per testing.ja.md tiers)

- **PR-0**: existing hook-render tests stay green unchanged (they are the
  behavior-preservation guard); no new tests unless coverage is missing.
- **Tier 1 (contract):** SSTI attempt (`__class__` traversal) → sandbox violation →
  `template_error`, nothing executed; strict-undefined error names missing vars;
  lenient records `undefined_vars`; output cap aborts a runaway `{% for %}` with a
  named-cap error; **producer neutrality**: ESC bytes survive in the op result and are
  stripped only when presented (falsify against producer-side stripping);
  read-authority equivalence (`template_ref` denied ⇔ `file.read` denied); present
  omission → stage-3 + ack `mode: "default"` + event `view: null`; renamed `view` arg
  round-trips and old `template` arg is rejected.
- **Tier 2 (OS invariant):** replay memo returns recorded render without re-executing;
  no write side effects.
- **No Tier-4 pins:** no exact rendered-whitespace assertions; assert content presence,
  error kinds, and meta fields.

## Decisions (owner deferred to architect recommendation, 2026-07-09)

1. **`undefined` default = `strict`** — confirmed. Loud-by-default (hard error naming
   the missing vars) protects file-generation sinks from silently writing broken
   artifacts; `lenient` (with `undefined_vars` meta) is one arg away.
2. **Sequencing** — PR-0 (env-helper pre-refactor) lands first and alone; PR-1 (present
   `view` rename + optional view) and PR-2 (`render_template` op, depends on PR-0) then
   proceed. PR-1 is independent of PR-2 and may land as soon as it is ready — no special
   fast-track needed.
3. Prior owner directions folded in: naming partition (`view` = declarative registered,
   `template` = Jinja2 text, `blueprint` unchanged), `render_template` op name kept,
   env-helper extraction as dedicated pre-refactor PR-0.

## References

- FP-0054 present layer (IMPLEMENTED) — declarative model, guard/renderer layering
  (Option B), stage-3 synthesis, `resolve_present_source`.
- `src/reyn/hooks/render.py` — SandboxedEnvironment precedent (#1800 slice B);
  strict/silent undefined per context; no size/time bounds (verified 2026-07-09).
- 0053 §3 — pipeline `ctx` full-value passing (no offload inside pipelines; owner
  ruling).
- A2UI — template-and-hydrate as the standard's token-saving mechanism; declarative-only
  by design: https://a2ui.org/introduction/agent-ui-ecosystem/ ·
  https://developers.googleblog.com/a2ui-and-mcp-apps/
- Follow-ups touched: #2663 (content_type sidecar — future auto markdown routing for
  rendered output).
