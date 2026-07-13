---
status: draft (2026-07-13)
owner-intent: ratified direction (owner GO 2026-07-13) — 2-layer structured output (ephemeral session → agent step) + agent-step model-class option; unsupported model errors, no silent degrade
---

# 0062 — Structured output for ephemeral sessions + pipeline agent steps

**One line:** expose the LLM funnel's already-present `response_format` (JSON-schema-constrained output) as an option on the **ephemeral session** (layer 1), then surface it — plus a **model-class** option — on the pipeline **agent step** (layer 2), so a pipeline author can declare `agent step: {output_schema: {…}, model: strong}` and get schema-validated JSON reliably.

## 0. Problem (owner framing)
A pipeline `agent` step's output cannot be reliably made into JSON — the step returns free-form model text, so a downstream `tool:`/`transform:` step that expects structured data breaks. The owner wants **structured output** as a first-class option. Their design instinct (adopted here): make it a feature of the **ephemeral session** first (the general layer), then let the agent step opt into it. Separately, the owner wants the agent step to also select its **model class**.

## 1. Grounded current state (file:line-verified on main, 2026-07-13)
- **The mechanism already exists.** `src/reyn/llm/llm.py` already threads `response_format: dict | None` through the LLM funnel (`_redact_llm_request_params` L1367, the `response_format` per-call routing param L1276, and a `fallback_without_response_format` retry L1538-9). So provider-side constrained generation is a plumbing-and-exposure task, not a build-from-scratch.
- **Schema generation exists.** `src/reyn/tools/task_ops.py:105` derives a JSON schema from a pydantic model via `model_json_schema()`; `structured_ref` (schemas/models.py:187) already models structured payloads. So both inline-JSON-Schema and pydantic-derived schemas are representable.
- **Ephemeral session spawn exists.** The pipeline `agent` step spawns a driver/ephemeral session under the invoker's identity (`pipeline_verbs.py` `_spawn_pipeline_driver_session` + `session_api._build_agent_step_narrowing`); `#2632` already forces `non_interactive=True` for ephemeral spawns.
- **Model resolution exists.** `reyn.llm.model_resolver` + model classes (`standard`/`light`/`strong`/…) — the agent-step model option reuses this, no new resolver.

## 2. Design

### 2.1 Layer 1 — ephemeral-session options: `output_schema` + `model`
The spawned ephemeral session gains two optional inputs:
- **`output_schema`** — a JSON Schema describing the session's FINAL output. When set, the session's terminal LLM turn (the one that produces the answer, not intermediate tool-loop turns) passes `response_format = {"type": "json_schema", "json_schema": {...}}` to `recorded_acompletion`. The final output is then **schema-validated on reyn's side** (belt-and-suspenders — Reliability lens: schema-validate + bounded re-prompt).
- **`model`** — a model-class override for the ephemeral session (resolved via `model_resolver`).

**Validation + failure policy:**
1. Provider-side constrained generation (`response_format`) is the first line.
2. reyn validates the returned JSON against `output_schema`. On failure → **bounded re-prompt** (feed the validation error back, N attempts, N small — reuse the existing schema-validate-and-re-prompt idiom the OS uses elsewhere). On exhausting the bound → a typed **error** (do not emit invalid/free-form output as if it were structured).
3. **Unsupported model → clear error, NO silent degrade** (owner-ratified). When `output_schema` is explicitly requested and the resolved model/provider does not support `response_format`, raise a typed error naming the model ("model `X` does not support structured output"). The existing `fallback_without_response_format` silent retry is **bypassed** whenever a schema is explicitly requested (it stays for non-schema calls).

### 2.2 Layer 2 — pipeline agent-step options
`AgentStep` (pipeline DSL model, `core/pipeline/models.py`) gains two optional fields:
- **`output_schema`** — threaded to the spawned ephemeral session's layer-1 `output_schema`. The validated JSON becomes the step's bound `output`.
- **`model`** — the model-class option (owner's second ask), threaded to layer-1 `model`.

So the DSL reads:
```yaml
- kind: agent
  prompt: "..."
  model: strong               # model-class option
  output_schema: { type: object, properties: { ... }, required: [ ... ] }
  output: my_binding          # receives the validated JSON
```

### 2.3 Schema format
**Inline JSON Schema** in the DSL (direct, no indirection; matches `response_format`'s json_schema shape). A **named-schema reference** (a registered schema by name) is a possible convenience add — deferred as a fork (§4a) unless authoring ergonomics demand it.

## 3. Reyn-lens fit
- **Tool Contract** — the step now emits a **typed, validated envelope** (schema-checked JSON) instead of an untyped string the LLM free-forms. This is squarely the lens's pass-line.
- **Reliability** — schema-validate + bounded re-prompt + typed error is the lens's own recovery idiom.
- **Product Think** — legible, predictable pipeline dataflow (a step's output type is declared, not hoped-for).
No new band-member obligations (no permission/WAL/audit surface change beyond the existing LLM-call audit).

## 4. Open forks (design co-vet + owner)
- **(a) schema format**: inline JSON Schema only (proposal) vs also a named-schema registry ref. Lean inline-first.
- **(b) re-prompt bound + who owns validation**: does validation/re-prompt live in the ephemeral-session run-loop (so ANY session with `output_schema` gets it) or in the agent-step wrapper? Proposal: **session run-loop** (layer 1 owns it, so the feature is genuinely session-level per owner's framing, and the agent step is a thin pass-through).
- **(c) output binding shape**: the validated JSON binds to the step `output` as a dict — confirm downstream `transform`/`tool` steps consume a dict cleanly (JSON-Pointer `$bind` etc.).
- **(d) partial/streaming**: structured output + streaming interaction (out of scope v1 — final-turn only).

## 5. Sequencing
1. **Layer 1** — ephemeral-session `output_schema` + `model` + validation/re-prompt/typed-error + unsupported-model error. Session-level tests (real spawn, schema pass/fail/re-prompt/unsupported).
2. **Layer 2** — `AgentStep.output_schema` + `AgentStep.model` fields + threading to layer 1 + DSL serde + pipeline tests (agent step produces schema-validated JSON bound to `output`).
3. Docs: the DSL agent-step reference + a "structured output" how-to.

## 6. Testing
- Layer 1: schema-valid output passes; schema-invalid triggers bounded re-prompt then typed error; unsupported model → typed error (not silent free-form); `model` override resolves + is used.
- Layer 2: agent step threads schema+model to the session; validated JSON binds to `output`; a downstream step consumes it.
- No golden snapshots outside `tests/scaffold/`.

## 7. Risks
- **Provider variance**: `response_format` support differs by provider/model; the unsupported→error policy makes this explicit (owner-accepted) rather than silently degrading.
- **Double validation cost**: provider-constrained + reyn-side validate is intentional (defense-in-depth); the re-prompt bound caps the cost.
