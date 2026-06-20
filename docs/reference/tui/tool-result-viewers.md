---
type: reference
topic: tui
audience: [human, agent]
---

# Tool-result viewers

The Right Panel events-tab preview renders a tool result (`tool_returned` /
`tool_executed`) richer than the generic YAML fallback — the Jupyter-style
"content-type → viewer" idea from #1154. A pluggable **viewer registry** maps a
result dict to a [Rich](https://rich.readthedocs.io/) renderable; when no
registered viewer matches, an **LLM-generated template** produces an adaptive
card for the unknown shape.

This page is the authoring/usage reference. The design rationale lives in the
proposal `docs/deep-dives/proposals/0051-tool-result-viewer-registry-llm-template.md`.

Module: `src/reyn/interfaces/tui/widgets/right_panel/tool_result_viewers.py`.
It is intentionally pure (dict in → renderable or `None`), so viewers are
unit-testable without the Textual app.

## How rendering is dispatched

There are two entry points:

| Function | Path |
|---|---|
| `render_tool_result(result)` | Sync. Walks the registry; returns the first match. |
| `render_tool_result_async(result, llm_client)` | Sync registry first, then LLM-template fallback. `llm_client=None` disables the LLM path. |

`render_tool_result` walks the ordered registry and **returns the output of the
first viewer whose predicate matches** — even if that output is `None`:

```python
for entry in _VIEWERS:
    if entry.predicate(result):
        return entry.viewer(result)   # first predicate match wins
return None
```

**Gotcha — make predicates precise.** Because the first matching predicate
"claims" the result, a predicate that matches but whose viewer returns `None`
does *not* fall through to the next registered viewer; the sync result is
`None` (the caller then shows YAML, or — on the async path — tries the LLM
template). So a predicate should only return `True` when its viewer can
actually render that result. Fold the "do I have the fields I need?" check into
the predicate, or accept that a matched-but-`None` viewer degrades to the
fallback.

## Authoring a concrete viewer

A viewer is two functions plus a registration call.

```python
from reyn.interfaces.tui.widgets.right_panel.tool_result_viewers import (
    register_viewer, _content_type_of, _result_text,
)

def _looks_like_thing(result: dict) -> bool:        # predicate: dict -> bool
    return _content_type_of(result) == "application/x-thing"

def _viewer_thing(result: dict):                     # viewer: dict -> RenderableType | None
    text = _result_text(result)
    if not text:
        return None                                  # nothing to show -> fall back
    from rich.panel import Panel
    return Panel(text)

register_viewer(_looks_like_thing, _viewer_thing, name="thing", position=0)
```

### `register_viewer(predicate, viewer, *, name="", position=-1)`

| Param | Type | Meaning |
|---|---|---|
| `predicate` | `Callable[[dict], bool]` | Return `True` if this viewer should render `result`. |
| `viewer` | `Callable[[dict], RenderableType \| None]` | Build the Rich renderable, or `None` to decline. |
| `name` | `str` | Label (used for de-registration in tests, and ordering inserts). |
| `position` | `int` | `-1` appends (lowest priority); `0` inserts at the front (highest). Any index inserts there. |

First match wins, so **order = priority**. The default population follows the
module convention: *explicit content-type viewers first, shape-sniff viewers
last.*

### `register_content_type_viewer(content_types, viewer, *, name, position=-1, match="exact")`

The ergonomic shortcut for the common case — *"this MIME maps to this viewer."*
It builds the `_content_type_of` predicate for you and delegates to
`register_viewer`, so `name` / `position` / first-match semantics are identical.

```python
from reyn.interfaces.tui.widgets.right_panel.tool_result_viewers import (
    register_content_type_viewer,
)

register_content_type_viewer("application/pdf", _viewer_pdf, name="pdf")            # exact
register_content_type_viewer("image/", _viewer_image, name="image", match="prefix")  # any image/*
register_content_type_viewer(("csv", "tab-separated"), _viewer_csv, name="csv",
                             match="substring")                                       # either, anywhere
```

| Param | Type | Meaning |
|---|---|---|
| `content_types` | `str \| Sequence[str]` | One MIME value, or several — a result matches if **any** matches. Case-insensitive. |
| `match` | `"exact" \| "prefix" \| "substring"` | How each value is tested against `_content_type_of(result)`. Default `"exact"`. |
| `name` / `position` | — | Same as `register_viewer`. |

Use this for a pure content-type check. Reach for `register_viewer` with a
hand-written predicate when you need more — a **suffix** test (the built-in
`markdown` viewer matches a `/md` suffix), a **shape-sniff** over dict keys
(`email` / `diff` / `web_summary`), or any multi-field heuristic.

### The predicate: detecting content type

Use `_content_type_of(result)` to read an explicit type. It checks, in order,
`content_type` / `mimeType` / `mime_type` / `media_type`, then the first
`media_blocks[0].mimeType`, and returns a lowercased string (or `""`).

```python
def _pred_json(r: dict) -> bool:
    ct = _content_type_of(r)
    return bool(ct) and "json" in ct
```

When a result carries no declared type, **shape-sniff** the dict instead — test
for a distinctive set of keys or a recognizable text payload:

```python
_WEB_SUMMARY_KEYS = ("title", "outline", "first_paragraph", "link_count")
def _looks_like_web_summary(r: dict) -> bool:
    return all(k in r for k in _WEB_SUMMARY_KEYS)
```

Prefer explicit-content-type predicates at a higher priority (lower index) than
shape-sniff predicates, so a declared type always wins over a heuristic guess.

### The viewer: building the renderable

The viewer returns any Rich `RenderableType` (`Table`, `Panel`, `Syntax`,
`Group`, `Markdown`, …) or `None`. Keep it pure — no Textual widgets, no app
state — so it stays unit-testable.

**Safety: result values are untrusted external content (#1822).** A tool result
can come from an MCP server, a fetched web page, or a file. If you place a value
into a Rich `Table`/`Text.from_markup`/any markup-parsing surface, escape it
first, or Rich will interpret embedded `[red]…[/red]` console markup:

```python
from rich.markup import escape
table.add_row("Subject", escape(str(value)[:500]))
```

Two markup-safe shortcuts that need **no** explicit escape:

- `rich.text.Text(value)` — constructs literal text (does not parse markup).
  (Note: `Text.from_markup(value)` *does* parse — don't use it on untrusted input.)
- `rich.syntax.Syntax(value, "lexer")` — renders through a pygments lexer; does
  not interpret console markup.

## Worked examples — email and diff

These two concrete viewers ship in the module and are the canonical examples.

### Email (header card)

```python
def _looks_like_email(result: dict) -> bool:
    # explicit RFC 822 type, OR a from+subject+(to|body) shape
    if _content_type_of(result) in ("message/rfc822",):
        return True
    return ("from" in result and "subject" in result
            and ("to" in result or "body" in result))

def _viewer_email(result: dict):
    from rich.console import Group
    from rich.markup import escape
    from rich.text import Text
    table = Table(show_header=False, box=None, expand=False)
    table.add_column("field", style="bold"); table.add_column("value")
    for label, key in (("From","from"),("To","to"),("Subject","subject")):
        val = result.get(key)
        if isinstance(val, str) and val.strip():
            table.add_row(label, escape(val[:500]))      # header: escape()
    body = result.get("body") or ""
    if body:
        return Group(table, Text(""), Text(body[:2000]))  # body: Text() (literal)
    return table
```

Header values are `escape()`-d; the body is wrapped in `Text` (literal). Both
are markup-injection-safe.

### Diff (syntax-highlighted)

```python
def _looks_like_diff(result: dict) -> bool:
    if _content_type_of(result) in ("text/x-diff", "text/x-patch"):
        return True
    text = _result_text(result)
    if not text:
        return False
    if text.lstrip().startswith("diff --git"):
        return True
    if "--- " in text and "+++ " in text:
        return True
    return "\n@@ " in text or text.startswith("@@ ")

def _viewer_diff(result: dict):
    text = _result_text(result)
    if not text.strip():
        return None
    from rich.syntax import Syntax
    return Syntax(text, "diff", theme="ansi_dark", word_wrap=False,
                  background_color="default")
```

`Syntax` is markup-injection-safe by construction (lexer, no markup parse).

Both are registered **before the generic JSON viewer** so an email/diff
delivered with `content_type: application/json` renders as a card, not a raw
JSON dump — while still sitting after the explicit markdown/csv content-type
viewers:

```
markdown, csv, [email, diff], json, image, web_summary
```

## The LLM-generated template (fallback for unknown shapes)

When no registered viewer matches, `render_tool_result_async` asks an LLM to
produce a display **schema** for the result's shape, then renders it through a
fixed, safe applier. This gives adaptive per-shape rendering without a
hand-built viewer — at a per-shape LLM cost + latency (so it complements, not
replaces, concrete viewers).

Pipeline:

1. **`_generate_template(result, llm_client)`** — sends only the result's
   **top-level key names** (never the values) to a cheap/fast model, asking for
   JSON: `{"rows": [{"label": "...", "field": "key"}], "caption": "..."}`.
   Returns `None` on any failure.
2. **`_parse_template_response(raw, valid_keys)`** — the safety fence:
   - JSON-only (`json.loads`); no `eval`/`exec`.
   - each `label` is `escape()`-d at construction.
   - each `field` must be a member of `valid_keys` (strict allowlist) — keys
     not present in the result dict are dropped.
   - rows capped at 8; caption capped at 40 chars.
   - any parse/type error → `None`.
3. **`TemplateSchema(rows, caption)`** — the parsed, escaped schema:
   `rows` is a list of `(escaped_label, field_key)` pairs.
4. **`_apply_template(result, schema)`** — renders the schema as a `Table`,
   `escape()`-ing each **value** at display time and skipping missing fields.

Caching: `_SHAPE_TEMPLATE_CACHE` maps a shape fingerprint (the frozenset of
top-level keys) to its `TemplateSchema` — or to `None`, which means "generation
failed for this shape; do not retry." So each distinct shape costs at most one
LLM call per session.

### Why two escape layers

The template is, by definition, an LLM-authored description that becomes Rich
markup. Both the **label** (LLM output, escaped at parse time) and the **value**
(untrusted result content, escaped at apply time) are escaped, and `field` is
constrained to an allowlist of real keys. The LLM never sees the values and
cannot emit `eval`/`exec` — it only picks labels and key names. This is the
defense-in-depth that lets an untrusted-shape result be rendered by
LLM-authored layout without a markup-injection or data-exfiltration path.

## Testing viewers

Viewers are pure, so render to a string and assert on content (no Textual app,
no golden files):

```python
from io import StringIO
from rich.console import Console
from reyn.interfaces.tui.widgets.right_panel.tool_result_viewers import render_tool_result

def _plain(renderable) -> str:
    buf = StringIO()
    Console(file=buf, highlight=False, markup=True, width=120).print(renderable)
    return buf.getvalue()

def test_email_card():
    out = _plain(render_tool_result({"from": "a@x", "subject": "Hi", "to": "b@y"}))
    assert "From" in out and "a@x" in out
```

For escape coverage, assert the literal bracket survives (markup *not*
interpreted): `assert "[bold]" in _plain(...)`. Register a custom viewer in a
test with `register_viewer(..., name="_test_x")` and remove it in a `finally`
via `_VIEWERS[:] = [e for e in _VIEWERS if e.name != "_test_x"]`.
