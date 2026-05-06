# B14-R1 Diagnosis — eval.run_target literal model string (B13-NEW-1)

| Field | Value |
|---|---|
| Date | 2026-05-06 |
| main HEAD | `2ea6302` |
| Batch | 14, R1 |
| Classification | 🔵 不具合修正 (bug fix, NOT spec change) |

## Reproduction result

Full end-to-end re-reproduction was not performed (would require LiteLLM proxy + 5-shot run). The
bug was confirmed at HEAD via static code analysis and matches the B13-S4 observation exactly.

B13-S4 session 3 observed:
> `litellm.BadRequestError: OpenAIException - {'error': '/chat/completions: Invalid model name`
> — model `gpt-3.5-turbo` rejected by proxy

The root cause is confirmed in HEAD code (described below).

## Model resolution path trace

### Full call chain

```
reyn chat → skill_improver.run_and_eval phase
  → run_skill op: { "skill": "eval", "model": "<session.model>" }  # e.g. "standard"
    → eval.run_target phase (OSRuntime; model="standard")
      → control_ir_executor._build_ctx(): ctx.model = "standard"  [HARDCODED]
        → LLM emits run_skill op: { "skill": "<target_skill_path>", "model": "gpt-3.5-turbo" }
          [LLM HALLUCINATED "gpt-3.5-turbo" in the model field]
          → run_skill.handle(): model = op.model or ctx.model = "gpt-3.5-turbo"
            → invoke_sub_skill(sub_skill, model="gpt-3.5-turbo", resolver=...)
              → Agent(model="gpt-3.5-turbo")
                → OSRuntime._effective_model(): "gpt-3.5-turbo" (no phase override)
                  → resolver.resolve("gpt-3.5-turbo") = "gpt-3.5-turbo"  [PASSTHROUGH]
                    → litellm.acompletion(model="gpt-3.5-turbo") → BadRequestError
```

### Key files

| File | Role |
|---|---|
| `src/reyn/op_runtime/run_skill.py` | Line 33: `model = op.model or ctx.model or "standard"` — no validation of `op.model` |
| `src/reyn/llm/model_resolver.py` | `resolve()`: unknown strings pass through unchanged (documented backward compat) |
| `src/reyn/kernel/control_ir_executor.py` | `_build_ctx()` line 244: `model="standard"` hardcoded |
| `src/reyn/stdlib/skills/eval/phases/run_target.md` | Invokes target skill via `run_skill` op — LLM may emit `model` field |

### Root cause

`ModelResolver.resolve()` passes unknown strings through unchanged. When the LLM in
`eval.run_target` emits `"model": "gpt-3.5-turbo"` in its `run_skill` op (hallucination
not prevented by phase instructions), the literal string reaches LiteLLM's proxy which
only has `light/standard/strong` classes configured. The proxy rejects `gpt-3.5-turbo`.

The `run_skill` op schema (`RunSkillIROp.model`) is documented as "model class or LiteLLM
string", which makes the LLM believe any model name is valid. The `run_target.md` phase
instructions do not explicitly prohibit the `model` field.

## Documented intent verification

`reyn.yaml` `models:` block:
```yaml
models:
  light:    openai/gemini-2.5-flash-lite
  standard: openai/gemini-2.5-flash-lite
  strong:   openai/gemini-2.5-flash-lite
```

`ModelResolver` docstring:
> Standard classes: light, standard, strong. Mapping is provided by ReynConfig.models.

`ReynConfig.model` default: `"standard"` — a model class, not a literal string.

Documented intent confirmed: **skills are expected to use model classes, not literal LiteLLM strings**.

## Chosen fix approach: Option A (skill-side) + resolver enforcement

**Rationale**: Option A alone (fixing `run_target.md`) is insufficient — it doesn't prevent
future phases from emitting literal model strings. Option B (resolver-side fallback) is the
structural fix: add `ModelResolver.is_known_class()` and use it in `run_skill.py` to reject
unknown model strings at the OS boundary.

### Changes

1. **`src/reyn/llm/model_resolver.py`**: Add `is_known_class(name: str) -> bool` method.

2. **`src/reyn/op_runtime/run_skill.py`**: Before using `op.model`, check
   `ctx.resolver.is_known_class(op.model)`. If False, log a warning and fall back to
   `ctx.model`. This enforces the model-class-only intent at the OS level.

This is a structural fix (P3 compliant): the OS enforces the model class contract, not
the LLM instructions. No skill-specific strings in OS code (P7 compliant).
