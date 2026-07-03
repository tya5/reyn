---
type: tutorial
topic: os-development
audience: [human]
---

# Add a new Control IR op kind

This tutorial walks through adding a new op kind to the Reyn OS end-to-end.
The goal is to make the 3-touch-point constraint tangible: after you finish,
the OS dispatches your op, the linter validates it in skill frontmatter, and
the LLM sees its schema in the system prompt — all without touching any skill
files or adding skill-specific strings anywhere.

**P7 in practice.** The OS's skill-agnostic guarantee (P7) is what makes this
possible. Op kind strings appear in exactly one place — `OP_KIND_MODEL_MAP` in
`schemas/models.py`. Every other mechanism (`ControlIRExecutor`,
`ALL_OP_KINDS`, `_build_phase_tool_catalog`) derives from that single source.
Adding a new entry to the map is sufficient; no scattered string literals to
track down.

---

## Before you start

Check that the op you're adding cannot be expressed as a sub-operation of an
existing kind (e.g. a new `file` sub-op like `file/compress` would go inside
`op_runtime/file.py`, not as a new top-level kind). New top-level kinds are
for fundamentally different execution semantics.

---

## Step 1 — Define the Pydantic model (`schemas/models.py`)

Every op kind has a typed Pydantic model that doubles as its JSON Schema for
the LLM. Add your model alongside the existing ones and include it in the
`Op` discriminated union.

```python
# src/reyn/schemas/models.py

class NotifyIROp(BaseModel):
    kind: Literal["notify"]
    channel: str        # delivery channel, e.g. "slack" or "email"
    message: str        # notification body
    severity: str = "info"   # "info" | "warning" | "error"

# Update the union — append NotifyIROp:
Op = Annotated[
    Union[
        FileIROp, MCPIROp, AskUserIROp, ShellIROp, LintIROp,
        RunSkillIROp, WebFetchIROp, WebSearchIROp,
        NotifyIROp,   # ← new
    ],
    Field(discriminator="kind"),
]
```

Rules for the model:
- `kind` must be a `Literal["<your_kind>"]`. This is the discriminator field.
- Field names must be generic (not skill-specific) — `channel`, `message`,
  `severity` rather than `slack_channel`, `alert_text`, `reyn_severity`.
- Default values for optional fields keep the LLM's minimal form short.

---

## Step 2 — Add to the op-kind map (`schemas/models.py`)

`OP_KIND_MODEL_MAP` lives in the same file as the model (#1983 co-location).
Add your kind alongside the model you defined in Step 1:

```python
# src/reyn/schemas/models.py

OP_KIND_MODEL_MAP: dict[str, type[BaseModel]] = {
    # The coarse "file" kind was retired in #1240 Wave 2b — file ops are now
    # the fine kinds read_file/write_file/edit_file/delete_file/glob_files/
    # grep_files (FileIROp is kept only as the shared execution backend).
    "read_file":  ReadFileIROp,
    "mcp":        MCPIROp,
    "run_skill":  RunSkillIROp,
    "shell":      ShellIROp,
    "lint":       LintIROp,
    "ask_user":   AskUserIROp,
    "web_fetch":  WebFetchIROp,
    "web_search": WebSearchIROp,
    "notify":     NotifyIROp,   # ← new
}
```

`ALL_OP_KINDS` is derived from `OP_KIND_MODEL_MAP.keys()` at module load — you
do not need to touch it.

---

## Step 3 — Implement the handler (`op_runtime/notify.py`)

Create a new file under `src/reyn/core/op_runtime/`. The handler is an `async`
function that takes the typed op and an `OpContext`.
It must return a JSON-serializable dict. Register it at module level so the
self-registration mechanism picks it up.

```python
# src/reyn/core/op_runtime/notify.py
"""notify op handler — send a notification to an external channel."""
from __future__ import annotations

from reyn.schemas.models import NotifyIROp

from . import register
from .context import OpContext


async def handle(
    op: NotifyIROp,
    ctx: OpContext,
) -> dict:
    ctx.events.emit("notify_started", channel=op.channel, severity=op.severity)

    try:
        # Replace with real delivery logic.
        await _deliver(op.channel, op.message, op.severity)
    except Exception as exc:
        ctx.events.emit("notify_failed", channel=op.channel, error=str(exc))
        return {"kind": "notify", "channel": op.channel, "status": "error", "error": str(exc)}

    ctx.events.emit("notify_completed", channel=op.channel, severity=op.severity)
    return {"kind": "notify", "channel": op.channel, "status": "ok"}


async def _deliver(channel: str, message: str, severity: str) -> None:
    """Stub — replace with real delivery."""
    raise NotImplementedError(f"no delivery backend configured for channel '{channel}'")


register("notify", handle)
```

Handler conventions:
- Emit `<kind>_started` and `<kind>_completed` events (P6 — every state change
  must be visible to the event log).
- Return `{"kind": op.kind, "status": "ok", ...}` on success.
- Catch exceptions and return `{"kind": op.kind, "status": "error", "error": str(exc)}`
  rather than raising — `execute_op` in `__init__.py` has a catch-all, but
  explicit handling produces cleaner events.
- Never import skill-specific modules or reference skill-specific strings
  (P7 — the OS must remain skill-agnostic).

Then add the import to `src/reyn/core/op_runtime/__init__.py` so the module
self-registers at startup:

```python
# src/reyn/core/op_runtime/__init__.py (end of file, with the other handler imports)
from . import notify as _notify  # noqa: F401, E402
```

---

## Step 4 — Update the reference doc (`docs/reference/runtime/control-ir.md`)

CLAUDE.md requires `control-ir.md` to stay in sync with `OP_KIND_MODEL_MAP` in
the same PR. Add a section for your op kind following the pattern of the
existing sections.

**In the op kinds table** (under `## Op kinds`), add a row:

```markdown
| `notify` | Send a notification to a configured channel | none |
```

**Add a dedicated section** at the end (before the contributor note):

```markdown
## `notify`

Sends a notification to an external channel. The channel must be configured
in `reyn.yaml` under `notify.channels:`.

​```json
{
  "kind": "notify",
  "channel": "slack-alerts",
  "message": "Build failed for skill my_skill",
  "severity": "error"
}
​```

Fields: `channel` (required), `message` (required), `severity` (optional,
default `"info"`; values: `"info"`, `"warning"`, `"error"`).
```

---

## Verifying your work

**Linter validation.** After adding to `OP_KIND_MODEL_MAP`, the linter
recognises `notify` as a valid op kind. A skill phase that declares
`allowed_ops: [notfiy]` (misspelled) will produce a lint error at compile
time, not a silent runtime skip.

Run the linter against any skill to confirm it loads without errors:

```
reyn lint reyn/local/my_skill
```

**Test the handler directly.** Write a Tier 1 contract test that calls
`execute_op` with a real `NotifyIROp` and a real `OpContext` (no mocks — see
`docs/deep-dives/contributing/testing.md`). Assert on the returned dict's
`status` field and on emitted events via `ctx.events`.

```python
"""Tier 1: notify handler returns ok result and emits expected events."""

async def test_notify_handler_emits_events():
    ctx = make_real_op_context()   # from your test helpers
    op = NotifyIROp(kind="notify", channel="test", message="hello")
    result = await execute_op(op, ctx)
    assert result["status"] in ("ok", "error")  # depends on stub behaviour
    event_kinds = [e.kind for e in ctx.events.to_list()]
    assert "notify_started" in event_kinds
```

**End-to-end.** Write a minimal skill phase that lists `notify` in
`allowed_ops` and run it with `reyn run`:

```yaml
# skill.md phase frontmatter
allowed_ops: [read_file, notify]
```

The LLM's system prompt will include the `notify` op schema (via
`_build_phase_tool_catalog`), and the LLM can now emit `notify` ops in its
`control_ir` list.

---

## What the OS gives you for free

After these four steps, no further OS changes are needed:

| Mechanism | How it picks up your op |
|---|---|
| `ALL_OP_KINDS` | Derived from `OP_KIND_MODEL_MAP.keys()` — updated automatically |
| `ControlIRExecutor` | Reads `_HANDLERS` which was populated by `register("notify", handle)` at import time |
| LLM system prompt | `_build_phase_tool_catalog` iterates `OP_KIND_MODEL_MAP` and derives JSON Schema from `NotifyIROp` |
| DSL linter | Validates `allowed_ops` entries against `ALL_OP_KINDS` |

The OS contains no `"notify"` string outside `schemas/models.py`. Any future skill
that wants to use notifications just adds `notify` to its `allowed_ops` — no
OS code changes, no new files outside the three touch points above.

---

## See also

- CLAUDE.md — P7 rationale and the detection rule
- [reference/runtime/control-ir.md](../../reference/runtime/control-ir.md) — op kind catalogue (must stay in sync)
- [guide/for-reyn-developers/principles-and-code.md](principles-and-code.md) — how each P-principle maps to code
- [deep-dives/contributing/testing.md](../../deep-dives/contributing/testing.md) — test tier requirements before writing tests
