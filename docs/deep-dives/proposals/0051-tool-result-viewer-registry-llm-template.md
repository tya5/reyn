# FP-0051: Tool-result viewer registry + LLM-generated viewer templates (#1154 Phase 3)

**[tui-coder]** — design-first proposal (2026-06-19). Status: **proposed** (awaiting lead review).

## Background

#1154 Phases 1–2c landed a content-type-aware tool-result preview in the TUI right
panel. Four viewers are live (markdown, CSV, JSON, image metadata, web-page summary),
dispatched by an inline `if/elif` chain in `render_tool_result`. Phase 3 has two goals:

1. **Registry seam** — replace the inline dispatch with a pluggable ordered registry so
   new viewers can be added without touching the dispatch function (P7 spirit: no
   hardcoded type strings in the dispatch core).
2. **LLM-generated viewer templates** — for results that match no registered viewer, let
   the LLM generate a *display schema* once per result-shape, cache it, and apply it on
   the next render. The LLM contributes structure (which fields, what labels), never
   data — fidelity is preserved.

## Current state (Phase 2c baseline)

```
tool_result_viewers.py
  render_tool_result(result: Any) → RenderableType | None
    ├── _content_type_of(result)
    ├── if "markdown" in ct → _viewer_markdown
    ├── elif "csv" in ct   → _viewer_csv
    ├── elif "json" in ct  → _viewer_json
    ├── elif ct.startswith("image/") → _viewer_image
    ├── elif _looks_like_web_summary → _viewer_web_summary
    └── else → None  (caller falls back to YAML)

right_panel/__init__.py:1197
  viewed = render_tool_result(result)   # sync, single call site
  if viewed: pane.show_text(title, viewed)
  else: pane.show_text(title, self._render_as_yaml(ev))
```

## S1 — Viewer registry seam (pure refactor, no behavior change)

Replace inline dispatch with a list of `_ViewerEntry` records:

```python
@dataclass
class _ViewerEntry:
    predicate: Callable[[dict], bool]
    viewer: Callable[[dict], RenderableType | None]
    name: str = ""          # debug label only

_VIEWERS: list[_ViewerEntry] = []   # populated at module load

def register_viewer(
    predicate: Callable[[dict], bool],
    viewer: Callable[[dict], RenderableType | None],
    *,
    name: str = "",
    position: int = -1,     # -1 = append (lowest priority)
) -> None:
    entry = _ViewerEntry(predicate=predicate, viewer=viewer, name=name)
    if position < 0:
        _VIEWERS.append(entry)
    else:
        _VIEWERS.insert(position, entry)

def render_tool_result(result: Any) -> RenderableType | None:
    if not isinstance(result, dict):
        return None
    for entry in _VIEWERS:
        if entry.predicate(result):
            return entry.viewer(result)
    return None
```

The existing Phase 1-2c viewers are registered as the default `_VIEWERS` list at module
load, in the same order as the current inline chain (behavior byte-identical).

**Why `position` parameter?** Priority matters: a new email viewer must fire before the
generic JSON viewer (email results often carry a JSON payload). The caller can insert at
a specific index rather than always appending.

**Seam contract**: `register_viewer` is the only public mutation point. `_VIEWERS` is a
module-level list (not a singleton class) — intentionally simple. Thread safety is not
required (TUI is single-threaded, viewer registration happens at import time).

## S2 — TemplateSchema + cache + safe apply (no LLM yet)

Data structures and the Rich builder; no LLM dependency in this step.

```python
@dataclass
class TemplateSchema:
    rows: list[tuple[str, str]]   # (escaped_label, field_key) pairs
    caption: str                  # escaped

# module-level cache; frozenset[str] → TemplateSchema | None
# None = "tried LLM, failed; use YAML"
_SHAPE_TEMPLATE_CACHE: dict[frozenset[str], TemplateSchema | None] = {}

def _shape_fingerprint(result: dict) -> frozenset[str]:
    return frozenset(result.keys())

def _apply_template(result: dict, schema: TemplateSchema) -> RenderableType:
    from rich.table import Table
    table = Table(show_header=False, box=None, expand=False)
    table.add_column("field", style="bold")
    table.add_column("value")
    for label, field_key in schema.rows:
        val = result.get(field_key)
        if val is None:
            continue
        table.add_row(label, str(val)[:500])   # hard cap: no blob dump
    if schema.caption:
        table.caption = schema.caption
    return table
```

Safety note on `_apply_template`:
- `label` is stored pre-escaped (escaped at schema construction time, see S3).
- `field_key` is validated against `result.keys()` at schema construction; applied here
  via `result.get(field_key)` — dict lookup only, no eval.
- `str(val)[:500]` caps displayed value length (prevents base64 blobs).

## S3 — Async LLM template generation

```python
async def _generate_template(
    result: dict,
    llm_client: Any,          # the session's LLM call surface
) -> TemplateSchema | None:
    """Call LLM once to produce a display schema for this result shape.

    Returns None on any failure (parse, validation, LLM error) — callers must
    treat None as "fall back to YAML, do not retry this shape".
    """
    keys = sorted(result.keys())
    prompt = (
        "You are a TUI display formatter. Given a tool result dict with these keys:\n"
        f"{keys}\n\n"
        "Respond with ONLY valid JSON — no prose, no code fences:\n"
        '{"rows": [{"label": "Human label", "field": "dict_key"}, ...], '
        '"caption": "short type description"}\n\n'
        "Rules:\n"
        "- Each \"field\" must be exactly one of the listed keys\n"
        "- Omit fields that are large blobs, base64 data, or internal IDs\n"
        "- Maximum 8 rows\n"
        "- caption must be ≤ 40 characters"
    )
    try:
        raw = await llm_client.complete(prompt, max_tokens=256)
        return _parse_template_response(raw, valid_keys=frozenset(keys))
    except Exception:
        return None

def _parse_template_response(
    raw: str,
    valid_keys: frozenset[str],
) -> TemplateSchema | None:
    """Parse and validate LLM JSON output → TemplateSchema or None."""
    import json
    from rich.markup import escape
    try:
        data = json.loads(raw.strip())
    except (ValueError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    rows_raw = data.get("rows")
    if not isinstance(rows_raw, list):
        return None
    rows: list[tuple[str, str]] = []
    for item in rows_raw[:8]:      # enforce max 8 rows
        if not isinstance(item, dict):
            continue
        label = item.get("label", "")
        field = item.get("field", "")
        if not isinstance(label, str) or not isinstance(field, str):
            continue
        if field not in valid_keys:
            continue               # strict: only known keys survive
        rows.append((escape(label), field))   # escape label at construction
    if not rows:
        return None
    caption_raw = data.get("caption", "")
    caption = escape(str(caption_raw)[:40]) if isinstance(caption_raw, str) else ""
    return TemplateSchema(rows=rows, caption=caption)
```

**Security properties of `_parse_template_response`**:

| Attack surface | Mitigation |
|---|---|
| LLM injects Rich markup in `label` | `rich.markup.escape()` at parse time |
| LLM names a non-existent field | `if field not in valid_keys: continue` — strict allowlist |
| LLM produces code (Python, JS, etc.) | JSON-only response; no eval/exec anywhere in the path |
| LLM produces giant label/caption | label: escape is applied before truncation is needed (Rich ignores markup tags, display length is bounded by terminal width); caption: `[:40]` hard cap before escape |
| LLM errors / times out | `except Exception: return None` → YAML fallback |
| JSON injection in LLM output | `json.loads` only; no string templates evaluated as Python |

**What the LLM can and cannot do**:
- CAN: pick which keys to show, assign human-readable labels, write a caption.
- CANNOT: introduce new string values into the display (all values come from `result.get(field)`), execute code, inject markup (escaped at construction), reference keys not in the result.

### Async entry point

```python
async def render_tool_result_async(
    result: Any,
    llm_client: Any,
) -> RenderableType | None:
    """Async variant: sync viewer registry first, then LLM fallback.

    Falls back to YAML (returns None) if both paths produce nothing.
    The sync render_tool_result() API is unchanged.
    """
    viewed = render_tool_result(result)
    if viewed is not None:
        return viewed
    if not isinstance(result, dict) or not result:
        return None
    fp = _shape_fingerprint(result)
    if fp in _SHAPE_TEMPLATE_CACHE:
        schema = _SHAPE_TEMPLATE_CACHE[fp]
        return _apply_template(result, schema) if schema is not None else None
    # Cache miss: generate, store, apply.
    schema = await _generate_template(result, llm_client)
    _SHAPE_TEMPLATE_CACHE[fp] = schema      # store None too (don't retry)
    return _apply_template(result, schema) if schema is not None else None
```

## S4 — Wire async path at call site

`right_panel/__init__.py:_show_event_in_preview` is already an async method (Textual
action). The call site switch:

```python
# Before (S1 sync baseline):
viewed = render_tool_result(result)

# After (S4 async with LLM fallback):
viewed = await render_tool_result_async(result, self._llm_client)
```

`self._llm_client` must be threaded into the right panel widget from the app. The exact
wiring path (constructor injection vs. `app.query_one` service locator vs. `post_message`
request) is a S4 implementation detail — to be decided at impl time based on how other
widgets access the LLM client.

**LLM client concern**: the right panel is TUI-only. We do NOT want to introduce a new
LLM call dependency if no session is active. Guard: if `llm_client is None` → skip async
path, return `render_tool_result(result)` only.

## Staged plan

| Step | Content | Behavior change | Review gate |
|---|---|---|---|
| **S1** | Registry seam refactor | None (byte-identical) | lead review + Tier-2 |
| **S2** | TemplateSchema + cache + `_apply_template` | None (not yet wired) | lead review + Tier-2 |
| **S3** | `_generate_template` + `_parse_template_response` + `render_tool_result_async` | None (not yet wired) | lead **security review** |
| **S4** | Wire async path at call site + llm_client injection | LLM fallback active | lead review + wire test |

S1 is a safe standalone merge. S2–S3 add code that is unreachable until S4 wires it.
S4 is the only step that changes user-visible behavior.

## Open questions for lead review

1. **llm_client surface**: which client type / call signature should `_generate_template`
   use? The TUI right panel doesn't currently hold a reference to the session's LLM
   surface. Best path: the app passes it at construction? Or we use a lightweight
   "generate display hint" OS op that doesn't require a full session?

2. **Cache persistence**: in-memory cache is lost on TUI restart. Persisting to
   `.reyn/viewer_templates/<fingerprint>.json` would make learned templates durable.
   Worth it in Phase 3, or deferred?

3. **Re-select UX**: first encounter → YAML (cache miss, async generate in background).
   Second encounter (user re-selects the same event) → rich view. Is this UX
   acceptable, or do we want a spinner + inline refresh?

4. **LLM model choice**: which model for template generation? A light/fast model is
   sufficient (the task is trivial — pick fields, write labels). Should the model be
   configurable, or hardcoded to `light`?

## Files

- **`src/reyn/interfaces/tui/widgets/right_panel/tool_result_viewers.py`** — S1–S3
  changes (registry + TemplateSchema + async generation)
- **`src/reyn/interfaces/tui/widgets/right_panel/__init__.py`** — S4 call site wire
- **`tests/cli/test_tool_result_viewer_registry.py`** (NEW) — S1 Tier-2 coverage
- **`tests/cli/test_tool_result_viewer_template.py`** (NEW) — S2–S3 Tier-2 coverage

## Tests (Tier-2)

**S1 (registry)**:
- `test_default_viewers_preserved_after_refactor` — all Phase 1-2c result fixtures pass
  through the new registry and return the same type as before (regression guard).
- `test_register_viewer_fires_before_fallback` — a custom viewer registered via
  `register_viewer` fires for a matching result; unmatched result falls through.
- `test_register_viewer_position_controls_priority` — a viewer inserted at position 0
  fires before the default viewers; appended viewer fires only when defaults don't match.

**S2–S3 (template)**:
- `test_apply_template_renders_rows` — `_apply_template` with a hand-crafted schema
  produces a Rich Table with the right label/value pairs.
- `test_apply_template_skips_missing_fields` — a field named in schema but absent in
  result is silently skipped (no KeyError).
- `test_parse_template_response_valid` — valid LLM JSON → TemplateSchema with correct
  rows.
- `test_parse_template_response_rejects_unknown_field` — a row whose `field` is not in
  `valid_keys` is dropped.
- `test_parse_template_response_escapes_label_markup` — a label containing `[bold]` is
  escaped; the plain text of the label does not contain Rich tags.
- `test_parse_template_response_invalid_json` — non-JSON response → None.
- `test_render_tool_result_async_uses_cache` — second call with same shape fingerprint
  does not call `_generate_template` again (cache hit).
- `test_render_tool_result_async_llm_failure_returns_none` — `_generate_template` raises
  → `None` returned, shape is cached as None (not retried).
