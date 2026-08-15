# Rules for `src/reyn/schemas/`

- **`docs/reference/runtime/control-ir.md` must stay synced with `OP_KIND_MODEL_MAP`** (`models.py`). New op kind → new section, same PR. **No CI checks this** — the CI-checked pair is `OP_KIND_MODEL_MAP` ↔ the `Op` union.
