---
status: done — landed same day (2026-07-13, commit `1bb8a2a0`, PR #2934)
owner-intent: ratified direction (owner GO 2026-07-13) — 2-layer structured output (ephemeral session → agent step) + agent-step model-class option; unsupported model errors, no silent degrade
---

# 0062 — Structured output for ephemeral sessions + pipeline agent steps

**One line:** expose the LLM funnel's already-present `response_format` (JSON-schema-constrained output) as an option on the **ephemeral session** (layer 1), then surface it — plus a **model-class** option — on the pipeline **agent step** (layer 2), so a pipeline author can declare `agent step: {output_schema: {…}, model: strong}` and get schema-validated JSON reliably.

## 0. Problem (owner framing)
A pipeline `agent` step's output cannot be reliably made into JSON — the step returns free-form model text, so a downstream `tool:`/`transform:` step that expects structured data breaks. The owner wants **structured output** as a first-class option. Their design instinct (adopted here): make it a feature of the **ephemeral session** first (the general layer), then let the agent step opt into it. Separately, the owner wants the agent step to also select its **model class**.

## 1. Grounded current state (file:line-verified on main, 2026-07-13; sharpened by docs-maintainer + architect co-vet)

> **Reframe (docs-maintainer finding — avoids reinventing an existing field):** `AgentStep` **already has a `schema` field**. This proposal is therefore an **enhancement of that existing `schema` path**, NOT a new parallel `output_schema` field.

- **`AgentStep.schema` ALREADY exists — but only VALIDATES post-hoc (this IS the owner's bug).** `AgentStep.schema: str | None` (`src/reyn/core/pipeline/executor.py:174`) names a `SchemaRegistry`-registered schema; `run_agent_step` (`src/reyn/runtime/session_api.py:191-217`) takes the agent's free-form reply, **JSON-parses + validates it POST-HOC** against the named schema, raising `AgentStepError` on non-JSON / non-conforming output. **It does NOT constrain generation** — the model free-forms, so when `schema` is set the text often isn't valid JSON → `AgentStepError`. *That is exactly why the owner "could not get JSON": the field validates but does not constrain.* The fix = make this existing path also drive provider-side constrained generation.
- **The constrained-generation mechanism already exists.** `src/reyn/llm/llm.py` threads `response_format: dict | None` through the funnel (`_redact_llm_request_params` L1367, per-call routing param L1276, `fallback_without_response_format` retry L1538-9).
- **Turn-isolation seam exists (architect pin).** `llm.py:1938` (ADR-0035 D2 **separate-decide**, tools XOR response_format) already isolates the **no-tools answer turn**. `response_format` attaches to that answer turn (tool-using agent) or to the sole turn (no-capability agent) — the clean existing injection point.
- **Capability pre-check exists (architect pin).** `litellm.supports_response_schema(model)` (NOT `supports_response_format` — that is False) → pre-check + typed error BEFORE the call for an unsupported model.
- **Schema registry / gen exist.** `SchemaRegistry` (named schemas, already wired to `AgentStep.schema`); `task_ops.py:105` `model_json_schema()`; `structured_ref` (schemas/models.py:187).
- **Ephemeral session spawn exists.** `src/reyn/runtime/session_api.py:314` `_spawn_pipeline_driver_session` + `_build_agent_step_narrowing`; `#2632` (commit ac1a67eb) forces `non_interactive=True` for ephemeral spawns.
- **Model resolution exists.** `reyn.llm.model_resolver` + model classes — the agent-step `model` option reuses this.

**No new Control-IR op kind** (AgentStep is pipeline-DSL schema, absent from `OP_KIND_MODEL_MAP`) → no `control-ir.md` hard-rule sync (docs-maintainer confirmed).

## 2. Design

### 2.1 Layer 1 — the ephemeral-session path: *enhance* `schema` to constrain generation + add `model`
The session-runner (`run_agent_step` + the session it drives) is where the existing `schema` post-hoc validation lives. We **enhance that same path** so a set `schema` ALSO constrains generation:
- **`schema` (existing, named)** — when set, resolve the named `SchemaRegistry` schema to a JSON Schema and pass `response_format = {"type":"json_schema","json_schema":{…}}` on the **separate-decide no-tools answer turn** (`llm.py:1938`, ADR-0035 D2 — the clean injection point; for a no-capability agent it is the sole turn, for a tool-using agent it is the answer turn after tools). The existing post-hoc JSON-parse + validate stays as belt-and-suspenders.
- **`model` (new — owner's 2nd ask)** — a model-class override for the ephemeral session (resolved via `model_resolver`).

**Failure policy — three DISTINCT modes (architect pins §2, §3):**
1. **Model does not support structured output → typed error via PRE-CHECK.** Before the answer turn, `litellm.supports_response_schema(model)` is checked; if False, raise a typed error naming the model ("model `X` does not support structured output"). Do NOT catch-classify a provider rejection for this (a raw 400 can't be reliably told apart from transient/other errors → misclassification). The existing `fallback_without_response_format` silent retry is **bypassed** whenever `schema` is set.
2. **The schema itself violates the provider's json_schema subset → typed error, NOT re-prompt.** A schema the provider rejects (e.g. OpenAI strict-mode: must be a root object, all-required-or-explicitly-optional, `additionalProperties:false`, no unsupported keywords) surfaces as a provider-side 400. **Re-prompting cannot fix an incompatible schema** — surface the provider's schema complaint as a typed authoring error (fail fast, no re-prompt loop).
3. **The model returns non-conforming JSON (generation-side) → bounded re-prompt, then typed error.** This is the reyn-side post-hoc validation failing on a *valid* schema — reuse the schema-validate-and-re-prompt idiom (feed the validation error back, N small attempts), then a typed error. Never emit invalid/free-form output as if structured.

### 2.2 Layer 2 — pipeline agent-step options
`AgentStep` (`src/reyn/core/pipeline/executor.py:174`) **already has `schema: str | None`** (named ref). We add ONE new field:
- **`model: str | None` (new)** — the model-class option (owner's 2nd ask), threaded to layer-1 `model`. The existing `schema` field now yields constrained + validated JSON bound to `output` (per §2.1) — no `output_schema` field is added.

So the DSL reads (the `schema` field is pre-existing; `model` is new; the *behavior* of `schema` upgrades from post-hoc-validate to constrain+validate):
```yaml
- kind: agent
  prompt: "..."
  model: strong            # NEW model-class option
  schema: my_schema        # EXISTING field — now constrains generation, not just validates
  output: my_binding       # receives the constrained+validated JSON
```

### 2.3 Schema format + strict-mode augmentation (pin)
Primary form = the **existing named `SchemaRegistry` ref** (`schema:`), enhanced per §2.1. **Inline JSON Schema** MAY be added as a second way to specify the same thing (feeding the identical response_format+validate path) — see fork §4a; not required for v1.

**Pin (architect finding §3):** decide whether reyn passes the resolved schema **verbatim** to `response_format` or **augments it to satisfy provider strict-mode** (inject `strict:true` / `additionalProperties:false` / coerce optional-field encoding). Verbatim is simpler but pushes provider-subset compliance onto the schema author (→ §2.1 failure mode 2); augmenting is friendlier but must not silently change validation semantics. **Proposal: verbatim in v1** + a clear failure-mode-2 error that tells the author what the provider rejected; augmentation is a follow-up if authoring friction proves high.

## 3. Reyn-lens fit
- **Tool Contract** — the step now emits a **typed, validated envelope** (schema-checked JSON) instead of an untyped string the LLM free-forms. This is squarely the lens's pass-line.
- **Reliability** — schema-validate + bounded re-prompt + typed error is the lens's own recovery idiom.
- **Product Think** — legible, predictable pipeline dataflow (a step's output type is declared, not hoped-for).
No new band-member obligations (no permission/WAL/audit surface change beyond the existing LLM-call audit).

## 4. Forks (design co-vet — 2 RESOLVED by architect, 2 open-minor)
- **(a) schema format** — OPEN (minor): named `SchemaRegistry` ref (existing, primary) only for v1, vs also add inline JSON Schema. Proposal: **named-only v1, inline as a fast-follow** (the named path already exists + is what we're enhancing; inline is additive). Crosses §2.3 (inline would still face the verbatim-vs-augment strict-mode pin).
- **(b) validation/re-prompt ownership** — **RESOLVED = Layer 1 (session-runner), separate-decide seam** (architect: `llm.py:1938` isolates the no-tools answer turn; Layer-1 ownership is clean, agent step is a thin pass-through). Owner's "session-level feature" framing honored.
- **(c) output binding shape** — **RESOLVED = low risk** (architect: existing `$bind`/JSON-Pointer already consumes dict output in present/render; one nested-dict `$bind` test confirms).
- **(d) partial/streaming** — out of scope v1 (final-turn only).
- **(e) `schema` verbatim vs strict-mode augment** — OPEN, pinned in §2.3 (proposal: verbatim v1 + clear failure-mode-2 error).

## 5. Sequencing
1. **Layer 1** — enhance the `run_agent_step` / session path so a set `schema` drives `response_format` on the separate-decide answer turn (`llm.py:1938`) + `supports_response_schema` pre-check + the 3 failure modes (§2.1) + `model` override. Session-level tests (real spawn: constrained-JSON pass, unsupported-model pre-check error, provider-schema-reject error, generation-side re-prompt-then-error).
2. **Layer 2** — add `AgentStep.model` field (the `schema` field already exists) + thread `model` to layer 1 + DSL serde for `model` + pipeline tests (agent step with `schema` produces constrained+validated JSON bound to `output`; `model` selects the class).
3. Docs: the DSL agent-step reference (document that `schema` now constrains, + the new `model` option) + a "structured output" how-to.

## 6. Testing
- Layer 1: schema-valid output passes; schema-invalid triggers bounded re-prompt then typed error; unsupported model → typed error (not silent free-form); `model` override resolves + is used.
- Layer 2: agent step threads schema+model to the session; validated JSON binds to `output`; a downstream step consumes it.
- No golden snapshots outside `tests/scaffold/`.

## 7. Risks
- **Provider variance**: `response_format` support differs by provider/model; the unsupported→error policy makes this explicit (owner-accepted) rather than silently degrading.
- **Double validation cost**: provider-constrained + reyn-side validate is intentional (defense-in-depth); the re-prompt bound caps the cost.
