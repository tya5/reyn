---
type: reference
topic: runtime
audience: [human, agent]
---

# Control IR

Control IR is the list of side-effect operations the LLM may emit. The OS dispatches each op and returns the result for the LLM to consume.

## Op kinds

| Kind | Purpose | Permission required |
|------|---------|---------------------|
| `read_file` | Read a file (optionally a line range) | `file.read` |
| `write_file` | Write (create / overwrite) a file | `file.write` |
| `edit_file` | Replace a string in a file | `file.write` |
| `delete_file` | Delete a file | `file.write` |
| `glob_files` | List files matching a glob pattern | `file.read` |
| `grep_files` | Search file contents by regex | `file.read` |
| `ask_user` | Pause the run and ask the user a question | none (always allowed) |
| `present` | Route bulk data + a declarative view to the user surface without the data passing through LLM output tokens (fire-and-continue) | Tier 0 (always allowed); `data_ref` read authority == `file.read` |
| `render_template` | Render a Jinja2 template against structured data into a string (a sandboxed producer — no side effects, no sink) | `template_ref` / `data_ref` read authority == `file.read`; inline-only is pure computation (no gate) |
| `sandboxed_exec` | Run argv under a `SandboxPolicy` via a `SandboxBackend` (replaces the removed `shell` op) | enforced by backend (`SandboxPolicy`) |
| `web_search` | Search the public web via DuckDuckGo | Tier 1 — default allow; `web.search: deny` in `reyn.yaml` blocks |
| `web_fetch` | Fetch a single URL and return extracted text | Tier 1 — default allow; `web.fetch: deny` in `reyn.yaml` blocks |
| `mcp` | Call a tool on a configured MCP server | `permissions.mcp: [server_name]` in skill frontmatter |
| `mcp_read_resource` | Read one resource (or a resolved resource-template URI) on a configured MCP server | `permissions.mcp: [server_name]` in skill frontmatter (same axis as `mcp`) |
| `mcp_subscribe_resource` | Subscribe to server-pushed `resources/updated` notifications for one resource URI (requires a persistent connection — see below) | `permissions.mcp: [server_name]` in skill frontmatter (same axis as `mcp`) |
| `mcp_unsubscribe_resource` | Cancel a previous `mcp_subscribe_resource` | `permissions.mcp: [server_name]` in skill frontmatter (same axis as `mcp`) |
| `mcp_get_prompt` | Fetch one rendered prompt (messages) by name from a configured MCP server | `permissions.mcp: [server_name]` in skill frontmatter (same axis as `mcp`) |
| `mcp_install` | Install an MCP server from the registry into the project config | `permissions.mcp_install: true` in skill frontmatter |
| `mcp_drop_server` | Remove an MCP server from project/local/user config (inverse of `mcp_install`) | `permissions.mcp_drop_server: true` in skill frontmatter |
| `skill_install` | Register a skill (local dir or git/URL source) into the project skills config | `file.write: [.reyn/config/skills.yaml]` in skill frontmatter; `http.get: [{host: <source_host>}]` when `source` is set |
| `load_skill` | Load a skill's `SKILL.md` body — the dedicated skill-activation verb (FP-0066 P0, #3247); expands invocation-time `${REYN_*}`/`${CLAUDE_*}`/`${env:VAR}` tokens for a registered skill | `file.read` |
| `pipeline_install` | Register a pipeline (local DSL file or git/URL source) into the project pipelines config | `file.write: [.reyn/config/pipelines.yaml]` in skill frontmatter; `http.get: [{host: <source_host>}]` when `source` is set |
| `presentation_install` | Register a named presentation template (inline blueprint) into the project presentations config | `file.write: [.reyn/config/presentations.yaml]` |
| `embed` | Raw embedding primitive: batch texts -> vectors (FP-0057 Phase 1; the user-facing primitive AND the shared logic later internal RAG ops call) | none (default-allow; embedding API cost) |
| `index_query` | Semantic vector search over one indexed source | none |
| `semantic_search` | Macro (FP-0057 Phase 2a; renamed from `recall`): per-source-model embed query → index_query per source → merge top-K (multi-model correct) | none (embedding API cost) |
| `index_drop` | Remove an indexed source entirely (destructive) | `permissions.index_drop: ask` in skill frontmatter |
| `index_update` | Incremental/delta-reconcile ingestion into a source's index (add/update/remove/skip; FP-0057 Phase 2a) | none (default-allow; own-write; embedding API cost) |
| `compact` | Voluntarily compact the conversation history (advisory) | none (LLM cost; the mandatory `retry_loop` backstop is independent) |
| `emit_hook_event` | Emit an LLM-authored `llm:<session_id>:<event_name>` hook-event onto the caller's OWN session `HookBus` (Hook-Event Redesign Phase 5 part 2) | none (structural session-binding + a static kind whitelist gate the autonomy boundary — see dedicated section below) |
| `describe_session` | Report this session's own position: write scope as DECLARED by the sandbox policy, repo/git/venv/toolchain facts, and auth status for reyn's own OAuth-managed providers (#5012-A) | none (read-only introspection) |

## Common envelope

Every op is a JSON object with a `kind` discriminator:

```json
{
  "kind": "read_file",
  "path": "src/foo.py"
}
```

The OS validates the op against its kind's schema, executes it, and returns a result to the LLM.

## File ops (fine-grained)

The LLM-emittable file operations are six fine-grained kinds — the same subset
the chat router exposes as tools (see
[concepts/architecture/llm-invocation-surfaces.md](../../concepts/architecture/llm-invocation-surfaces.md)).
Each is a distinct op kind with its own schema; there is no `op` sub-field.

```json
{"kind": "read_file", "path": "src/foo.py"}
{"kind": "read_file", "path": "src/foo.py", "offset": 100, "limit": 40}

{"kind": "write_file", "path": "out.txt", "content": "..."}

{"kind": "edit_file", "path": "src/foo.py",
 "old_string": "...", "new_string": "...", "replace_all": false}

{"kind": "delete_file", "path": "tmp.txt"}

{"kind": "glob_files", "path": ".", "pattern": "**/*.py", "max_results": 50}

{"kind": "grep_files", "path": "src", "pattern": "def \\w+",
 "glob": "**/*.py", "case_sensitive": false, "max_results": 50}
```

| Kind | Permission | Notes |
|------|-----------|-------|
| `read_file` | `file.read` | `offset` / `limit` (line range) optional. A plain read — no path/filename special-casing: a `SKILL.md`-named file reads byte-identical to any other file (the former invocation-time `${REYN_*}`/`${CLAUDE_*}`/`${env:VAR}` expansion pass for exactly that filename moved to the dedicated `load_skill` op below, FP-0066 P0/#3247 — see that op's section). When the inline cap self-bounds the read, the result carries `status: "truncated"` and a `note` (chars shown of total + the on-disk path/offset to resume from); the chat router's `read_file` alias, which otherwise flattens the result to a bare string before `to_canonical` runs, appends that same `note` inline instead of dropping it (#3191). `char_offset` (int, optional, #2335) — the char position WITHIN the line named by `offset` to resume from; set ONLY on a truncated result whose single overflowing line was itself longer than the inline cap (the result's own `next_char_offset`), never on an ordinary multi-line truncation (which resumes at a LINE boundary via `next_offset` alone, `char_offset` absent — pre-#2335, byte-identical). Passing `offset`/`limit` together with `char_offset` set switches the read out of the plain line-window mode above into the self-bounding accumulate-from-`(offset, char_offset)` mode. |
| `write_file` | `file.write` | Creates or overwrites; parent dirs created as needed. |
| `edit_file` | `file.write` | `old_string` must be unique unless `replace_all: true`. |
| `delete_file` | `file.write` | |
| `glob_files` | `file.read` | `path` defaults to `.`. `max_results` defaults to 50 (unchanged). When it discards matches, the result additionally carries `truncated: true`, `total_count`, and `returned_count` (#2998) — the chat router's `list_directory` alias (fine-grained `glob_files` under a synthesized `<path>/*` pattern) surfaces the same fact as a trailing note instead of the frontmatter meta path this result rides. |
| `grep_files` | `file.read` | `glob` filters which files are searched. |

Permission scopes are configured per-op kind. See `reference/config/permissions.md`.

A successful `edit_file` result additionally carries a `preview` (str): a
numbered-line view (`<lineno>\t<text>`, 1-based) of the changed region — the
lines around where `new_string` landed (±3 by default), so the agent can SEE
*what* changed and at what indentation, not just `{status, replacements}`. It is
**show-not-judge** (numbered lines only — no syntax check or validity verdict),
**language-agnostic** (pure line slicing), and bounded (capped height). For
`replace_all` it shows the first changed region; the count is in `replacements`.

### The coarse `file` execution backend (not LLM-emittable)

The fine kinds above are the only file ops advertised to (and accepted from)
the LLM. They are dispatched through the unified ToolRegistry, then build
a coarse `FileIROp` (`{kind: "file", op: ...}`) internally and route to the
shared `op_runtime/file.py` backend. That coarse `file` kind — dropped from
`OP_KIND_MODEL_MAP` — is **not** an LLM-emittable Control IR
kind. It survives only as:

- the shared execution backend the fine handlers delegate to, and
- the target of OS-deterministic preprocessor `run_op` steps
  (`{kind: file, op: ...}`), the chat host file methods, and the `reyn memory`
  CLI.

Those non-LLM callers also reach extended sub-ops the fine kinds do not
expose — `mkdir`, `move`, `stat`, and `regenerate_index` (used by `reyn memory`
and memory-managing skills via the preprocessor / CLI, never as an LLM-emitted
Control IR op).

## `ask_user`

Pauses the run and asks the user. The OS routes the question through the intervention bus (`ChatInterventionBus` for the inline CUI, `StdinInterventionBus` for CLI) and resumes once the user answers.

```json
{
  "kind": "ask_user",
  "question": "Which model do you want to target?",
  "suggestions": ["light", "standard", "strong"],
  "options": ["light", "standard", "strong"],
  "required": true
}
```

`suggestions` are free-text hints (the user may still type anything). `options` (PR-F3, #2233) is a **closed selectable set** — when non-empty, the frontend renders a **selector** over exactly those answers (empty → free-text input). `required` (default `true`) — when `false`, the user may dismiss without answering.

## `present`

Routes bulk data plus a declarative view to the user-facing surface
**without the data round-tripping through LLM output tokens**. The offloaded ref
file is already "data file + handle"; `present` joins that handle to a view
so the bulk bytes reach the user directly. Presenting N rows costs ~0
output tokens; the moment the agent must *transform* the data it pays to read the
ref instead.

**Tier 0** (`ask_user`'s sibling): presenting to the user — the trust root — is
not an exfiltration channel, so there is no output permission gate. The one gate:
`data_ref` read authority resolves **identically to `file.read`** — `present` can
never read more than the agent's file ops can. Unlike `ask_user`, `present` is
**fire-and-continue** — it does NOT pause the run.

```json
{
  "kind": "present",
  "data_ref": ".reyn/cache/tool-results/2026-.../structured.json",
  "blueprint": {
    "component": "table",
    "rows": {"$bind": "/results"},
    "columns": [
      {"header": "Title", "path": "/title"},
      {"header": "Author", "path": "/author"}
    ]
  }
}
```

Fields (exactly one source; at most one of `view` / `blueprint` — both omitted is
valid, see the PR-1 note below):

- `data_ref` (str) **XOR** `data_inline` (any) — the data source. `data_ref` is
  any zone-readable path; an offloaded `structured_ref` is **re-hydrated to its
  full value** (not read from the LLM-visible preview) via `file.read` semantics.
  `data_inline` is small data already in the LLM's context.
- `view` (str) **at most one with** `blueprint` (object | array) — the view.
  `view` is a registered presentation name (the registry + fallback chain,
  see the PR-B/C/D note below); `blueprint` is an inline declarative component tree.
  (FP-0055 PR-1 renamed this arg from `template` — a clean break, no alias — as
  part of a vocabulary partition: `view` is the declarative sense, `template` is
  reserved for the `render_template` op's Jinja2 text templates.)
- **Both omitted (FP-0055 PR-1):** valid — "no explicit view" routes straight to
  the stage-3/4 default-viewer synthesis below; `present(data_ref=...)` alone
  "just shows" the data.

**Declarative model (v1 catalog — display-only, non-executable by construction).**
A blueprint is a single component node or a list of them (rendered top to bottom).
Catalog components (all read-only): `text` / `markdown` / `code` / `diff` /
`keyvalue` / `table` / `list` / `image`. There are **no interactive components**
(no buttons / forms) in v1. Bindings are expressed structurally as
`{"$bind": "<json-pointer>"}` — an RFC 6901 JSON Pointer **string** (`""` = whole
document); everything else is a literal. `table` / `list` column paths resolve
**row-relative** (relative to each iterated row). The structural gate at op
validation rejects a non-catalog component or a non-path binding (a hard error,
not a soft drop); it is purely structural — leaf-string neutralization is a single
seam in the render layer (below), not at parse.

**Binding semantics.** Path hit → bind. Path miss → **soft-skip** that binding +
record it in `bindings_dropped` (never a hard failure). Type mismatch → coerce (a
scalar into a `table` `rows` slot → a 1-row table; a container into a `text` slot
→ its JSON form) + record `{path, rendered_as}` in **`coerced`**, NOT
`bindings_dropped` — a coercion is the *opposite* outcome of a drop, the value
reached the user reshaped rather than never reaching it (#3664). Guard-stripped →
a bound leaf neutralized or size-capped by the presentation-guard is recorded in
`bindings_dropped`. When **all** bindings miss, the op reports `all_bindings_missed`
(the generic-viewer fallback signal; the fallback wiring itself is described in
the PR-B/C/D note below).

**Presentation-guard (output seam).** Runs **unconditionally**, including for
never-ingested data. Every render-leaf string — labels, literal slot values, AND
bound data values — passes through ONE neutralizer, selected by the target
**surface** (a per-surface strategy, so a future web surface slots in without
touching the binding layer). The v1 **terminal** strategy strips ESC / control
sequences (OSC / CSI) only; it does **not** escape Rich console markup and does
**not** HTML-escape. Rich-markup safety is deliberately NOT this seam's job (PR-B
revision): Rich console-markup injection is reachable only through
`console.print(str, markup=True)` — a choice the *renderer* makes per Rich
object, not a property of the terminal sink. The inline-CUI renderer routes
every leaf into a markup-inert Rich object (`Text` / `Syntax` / `Markdown`) and
never calls `console.print` with markup interpretation on presented content, so
Rich injection is structurally impossible regardless of what the guard does — the
same "safety from shape, not policy" discipline as the guard's own ESC-strip.
HTML neutralization stays a future web renderer's own concern (in a terminal
`<div>` is a harmless literal, and entity-escaping would corrupt `code` / `diff`
content). **Per-binding size caps** prevent a `/` (root) pointer bound into a
`text` component from dumping a whole file. Neutralization is a transform (the
value still renders, inert) — the ref remains the full-fidelity source.

**Ack (op result)** — the LLM's only feedback, deliberately compact + high-signal:

```yaml
ok: true
mode: view        # view | blueprint | default (FP-0055 PR-1) — which input the caller gave
bindings_resolved: 3
bindings_dropped:
  - {path: "/results/0/author", reason: path_not_found}
  # reason ∈ {path_not_found, guard_stripped} — a type-mismatch coercion is NOT
  # a drop (#3664); see `coerced` below
coerced:
  - {path: "/big", rendered_as: json_text}
  # rendered_as ∈ {json_text, single_row} — the value WAS displayed, reshaped
rows: 500          # #3664: every rendered row across every rows-shaped slot
                    # (table/list AND keyvalue), post-cap — the LLM's only
                    # measure of how much reached the user's screen
all_bindings_missed: false
```

`path_not_found` across many rows reads as "view doesn't match this data
shape"; `guard_stripped` as "content neutralized by the guard, not a view bug";
a `coerced` entry as "right path, wrong component — this reached the user
reshaped, not dropped". The LLM self-corrects a blind presentation for tens of
tokens without ingesting the data. With `mode: "default"` (neither `view` nor
`blueprint` given) the stats above are the synthesized default viewer's own —
this is the intended rendering, so there is no fallback `note` unless that
default viewer itself degrades further to the stage-4 generic fallback, or
(#3664) coerces a container to JSON text — the same self-correction signal
`all_bindings_missed` gives the `view`/`blueprint` path, which this path
otherwise has none of.

Event emitted: `presented` (P6 audit) — `{data_ref, view, mode, surface, ingested,
bindings_resolved, bindings_dropped, coerced, rows, fallback_stage}`. `view` is the registered name,
`blueprint:<hash>` for an inline blueprint, or `null` when neither was given.
`fallback_stage` (`null` | `content_type_default` | `generic`) records which viewer actually
reached the user — `null` when the requested rendering rendered directly, else the synthesized
fallback stage — so a literal-only view (rendered as requested) is distinguishable from an
unknown / all-missed view that fell through, which otherwise share `bindings_resolved=0`.
`ingested` (`none` | `partial` |
`full`) is **OS-computed** (was the data inline, or does a `read_file` on the ref
appear earlier in the session), never LLM-self-reported. The event carries **refs
+ stats only, never content bytes** (the data is already durable in the ref).

> PR-B: the inline-CUI renderer is wired (`surface: ["inline-cui"]` when a chat
> session's `OpContext.presentation_renderer` is set; `["null"]` otherwise — e.g.
> a bare `OpContext` built without one, PR-A's original behavior). It renders
> `ResolvedPresentation.nodes` as a one-shot inline block in the conversation
> scrollback (`interfaces/repl/present_renderer.py`, riding the existing Rich
> `Console` → `StringIO` → `run_in_terminal()` pattern), with an explicit
> per-render terminal width (Rich cannot auto-detect width writing to a
> `StringIO`). The `presentations.yaml` registry + 4-stage fallback chain and
> replay/rewind re-rendering are landed. On replay (`reyn events <log>`) a
> `presented` event re-renders best-effort from the still-durable ref, or shows an
> expiry placeholder pointing at the audit event when the ref is gone — a
> display-only projection (no reconstructed state). See
> [Concepts: Present layer](../../concepts/runtime/present.md) and the
> [Present op & surface reference](present.md) for the full surface.

## `render_template`

Renders a Jinja2 template against structured data into a plain string. A general,
sandboxed **producer**: `data + template → string`, with **no side effects and no
sink** — the rendered string is returned as an ordinary op result (canonical `text`;
large output auto-offloads on the chat path). The caller routes it to whatever sink it
wants: `present`, a `write_file`, a message body, or a pipeline `ctx`.

Prefer `present` (declarative) to show structured data to the user — it is
token-economical and portable. Reach for `render_template` only when you need
**computed text**: loops / conditionals / aggregation woven into prose, which
declarative binding intentionally cannot express.

```json
{
  "kind": "render_template",
  "template": "{% for r in data.results %}- {{ r.title }}\n{% endfor %}",
  "data_ref": "runs/summary.json",
  "undefined": "strict"
}
```

Fields:
- `template` (XOR `template_ref`) — inline Jinja2 source string.
- `template_ref` (XOR `template`) — a zone-readable template file path, read as **raw
  text** under `file.read` authority (a template file is source text, never
  JSON-rehydrated).
- `data_ref` (XOR `data_inline`) — any zone-readable path, re-hydrated to its full
  value under `file.read` semantics (the same seam `present` uses).
- `data_inline` (XOR `data_ref`) — a small object already in the LLM's context.
- `undefined` (optional, default `"strict"`) — `"strict"`: an undefined variable is a
  **hard error naming the missing name** (loud-by-default, so a file sink never
  silently writes a broken artifact); `"lenient"`: undefined renders empty and the
  referenced-but-unbound names surface as `undefined_vars` in the result meta.

The resolved data binds under **`data`** in the template context
(`{{ data.results[0].title }}`).

**Sandbox + neutrality.** The engine is always `jinja2.sandbox.SandboxedEnvironment`
(via the one factory, `reyn.security.template_env.make_sandboxed_env`) — templates may
be LLM-authored, and unsandboxed Jinja2 is arbitrary-code execution (SSTI). A blocked
attribute traversal (`{{ ().__class__ }}`) raises a sandbox violation → a structured
`error` result; nothing executes. `autoescape` is **OFF**: the op returns RAW rendered
bytes. Neutralization is the **sink's** job (a terminal strips control bytes at its
guard, a file is inert, a web surface HTML-escapes) — escaping in the producer would
corrupt file / terminal artifacts.

**Read-authority equivalence.** `template_ref` / `data_ref` resolve through exactly the
`file.read` gate; a denied read → `status="denied"`. render_template can never read
more than the agent's `file.read` can. An inline-only invocation (`template` +
`data_inline`) is pure computation — no read gate.

**Resource bounds.** `SandboxedEnvironment` stops SSTI but not resource exhaustion — a
bounded loop like `{% for i in range(10**9) %}` still floods. The cap is applied
**during** generation (streaming `template.generate(context)`), accumulating against a
max-output-chars budget with a wall-clock backstop; the moment either is exceeded the
render stops and the result is TRUNCATED with a `truncated: true` meta flag naming
which bound fired (`truncate_reason`) — a bounded result, never an OOM / hang. Bounds
default to safety-spirit constants (operator-tunable via `OpContext.render_template_bounds`).

Result fields: `rendered` (the string), `truncated`, `truncate_reason` (when
truncated), `undefined_vars` (lenient mode). An error result carries
`status="error"` + `error_kind` (`template_error` | `security` | `undefined`) +
`error`. No new event type — standard op events; a pure function of (template, data),
so ordinary memo/replay applies.

## `sandboxed_exec`

Executes `argv` under a declared `SandboxPolicy` via the OS's selected `SandboxBackend`. Replaces `shell` for cases that need (or will need, once `SeatbeltBackend` / `LandlockBackend` land) real isolation enforcement.

The Control IR op kind stays `sandboxed_exec` (`OP_KIND_MODEL_MAP["sandboxed_exec"]` / `SandboxedExecIROp`). The router/phase tool that reaches this op was renamed `sandboxed_exec` -> **`exec`** (#3226 Phase 3) — the rename is tool-name-only and does not touch this op schema, its events, or its result shape. The op kind and the tool name therefore differ. A table bridging the two strings (`op_runtime.contextual_gate._OP_KIND_TOOLS`) used to exist; #3513 deleted it, because its only consumers — `control_ir_executor` / `preprocessor_executor` — had already been deleted as whole files in #2434, leaving it with no caller in `src/`. Whether op dispatch requires contextual narrowing in its own right is tracked in #3546 and is not settled here.

```json
{
  "kind": "sandboxed_exec",
  "argv": ["echo", "hello"],
  "stdin": null,
  "timeout_seconds": null
}
```

Fields:
- `argv` (required) — command + arguments. `argv[0]` is the executable.
- `stdin` (optional, default `None`) — bytes written to the process's stdin, if any (a pipeline `tool` step can thread the previous step's pipe-data here as JSON via `args: {argv: [...], stdin_pipe: !expr pipe}` — see [Pipeline DSL](pipeline-dsl.md#tool)).
- `timeout_seconds` (optional, default `None`) — **#3903① (2026-08-11), a
  deliberate reversal of the paragraph below**: the LLM may request a
  foreground wall-clock timeout above `SandboxPolicy.timeout_seconds`'s own
  default, up to `SandboxPolicy.max_timeout_seconds` — the operator's OWN
  configured ceiling, never a hardcoded value (an operator who narrows
  `max_timeout_seconds` below its 600s default has that ceiling actually
  enforced; the LLM cannot widen an operator's own narrower configuration).
  `None` (the default) uses the policy's own `timeout_seconds`. A value
  above the ceiling is **rejected** (`status: "error"`, naming the actual
  configured max) — never silently clamped, which would recreate the
  advertised-but-ignored shape the removal below closed, just for a value
  that quietly changes instead of a field that's quietly dropped. A
  non-positive value is rejected the same way.

**No other policy fields** (`network` / `read_paths` / `write_paths` /
`allow_subprocess` / `env_passthrough` — removed #3907): the sandbox
policy's other axes that actually govern a run are **never** settable via
this op. It is the agent-level (operator) `sandbox.policy`
(`reyn.yaml`, resolved through `resolve_sandbox_policy` — see the
[`sandbox` config block](../config/reyn-yaml.md#sandbox-block)) or, absent that, the
operator's compat/strict default — either way a value the LLM cannot see or widen
(#1326/#1339: the operator-or-default policy always wins over anything an op
requests). The op used to carry the 5 policy fields as a fallback source when
no operator policy was resolved; #3907① measured that path is unreachable in
production (every context-building path resolves a concrete policy), so the
fields were pure LLM-facing surface with no effect — deleted rather than left
as an advertised-but-ignored knob.

`timeout_seconds` followed the same path once (removed #3962 as the same
defect class, one field late — a wall-clock cap isn't a permission axis, so
it survived #3907's sweep and stayed dead one issue longer) but is a
DIFFERENT axis from the 5 above: boundedness, not permission (#3903's own
framing). It came back with a real reader this time — see above — which is
what distinguishes this reversal from reopening the gap #3962 closed.

**Backend selection**: `get_default_backend()` chooses per platform. On macOS < 26, `SeatbeltBackend` (sandbox-exec SBPL). On Linux ≥ 5.13 with the `sandbox-linux` extra installed, `LandlockBackend` (+ optional seccomp-BPF stack). On other platforms or when the chosen backend is unavailable, falls back to `NoopBackend` (audit-only, no enforcement) — emits a one-line WARN on first use. Override via `reyn.yaml` `sandbox.backend` (`auto` | `seatbelt` | `landlock` | `noop`) and `sandbox.on_unsupported` (`warn` | `error` | `ignore`).

Result fields: `returncode`, `stdout`, `stderr`, `truncated`, `backend`.

Events emitted: `sandboxed_exec_started`, `sandboxed_exec_completed` (P6 audit trail).

## `web_search`

Searches the public web using DuckDuckGo and returns structured results. **Tier 1** — default allow; no permission declaration required. Can be blocked project-wide with `web.search: deny` in `reyn.yaml`.

```json
{
  "kind": "web_search",
  "query": "reyn agent OS site:github.com",
  "max_results": 10,
  "backend": "duckduckgo"
}
```

Fields: `query` (required), `max_results` (optional, default `10`), `backend` (optional, default `"duckduckgo"`; currently the only supported value).

Standard DuckDuckGo search operators are supported in `query`:

- `site:<domain>` — scope results to one domain (e.g. `site:news.ycombinator.com`)
- `"phrase"` — require exact phrase match
- `-term` — exclude results containing `term`

Use operators when the user's intent is site-specific or phrase-anchored; plain keywords work otherwise. Results are returned as a list of `{title, url, snippet}` objects under `results`.

## `web_fetch`

Fetches a single URL and returns its text-extracted content. **Tier 1** — default allow; no permission declaration required. Typically used after `web_search` to read a result page in detail. Block with `web.fetch: deny` in `reyn.yaml`; pre-approve silently with `web.fetch: allow`.

```json
{
  "kind": "web_fetch",
  "url": "https://example.com/article",
  "prompt": "extract the key findings"
}
```

Fields: `url` (required), `prompt` (optional hint describing what to extract — informational for the LLM, not executed by the OS), `timeout` (optional, default `30` seconds).

HTML responses are text-extracted (scripts, styles, and non-content tags stripped); non-HTML responses are returned as-is. **The extracted text is returned whole — `web_fetch` has no size cap of its own** (#3580: the `max_length` argument and the `truncated`/`next_start`/`start_index` paging fields were removed; the `start_index` half never reached the tool schema, so the LLM could never page). Two other bounds still apply: `web_fetch.max_download_bytes` in `reyn.yaml` (#4174 T4, renamed from `web.fetch.max_download_bytes`) caps the HTTP body downloaded (default 10 MiB, rejected as `status: "too_large"`), and how much of the result reaches the model's context is governed by the OS-level tool-result cap (`offload.enabled`, default `false` = uncapped) — not by this op.

## `mcp`

Calls a tool on a configured MCP server. Requires the server to be declared in `reyn.yaml` under `mcp.servers:` **and** listed in the skill's `permissions.mcp` frontmatter block.

```json
{
  "kind": "mcp",
  "server": "filesystem",
  "tool": "read_text_file",
  "args": {"path": "README.md"}
}
```

Fields: `server` (required — must match a key under `mcp.servers:` in `reyn.yaml`), `tool` (required — tool name as advertised by the server's `tools/list` response), `args` (optional, default `{}`).

> **Advertised name.** Phases advertise this op to the LLM under the chat-tool
> name `call_mcp_tool`; the OS aliases it back to the `mcp` kind at the parse
> boundary. `mcp` remains the canonical kind in `OP_KIND_MODEL_MAP` and on the
> dispatched op.

The OS resolves the server's transport (`stdio`, `streamable-http`, or `sse`), dispatches via `MCPClient`, and returns the tool result. Every call emits `mcp_called`, `mcp_completed`, and (on failure) `mcp_failed` events.

See [concepts/tools-integrations/mcp.md](../../concepts/tools-integrations/mcp.md) for server configuration, transport options, and the security model.

## `mcp_read_resource`

Reads one resource (or a resolved resource-template URI) from a configured MCP server. #2597 slice ②a (resources consumption) — gated by the **same** `permissions.mcp` axis as `mcp` (call_tool): a resource read returns external, potentially sensitive server-authored content, so it is permission-gated identically to a tool call.

```json
{
  "kind": "mcp_read_resource",
  "server": "filesystem",
  "uri": "file:///README.md"
}
```

Fields: `server` (required — must match a key under `mcp.servers:` in `reyn.yaml`), `uri` (required — a resource URI as advertised by the server's `resources/list`, or a resolved `resources/templates/list` template).

> **Advertised name.** Phases advertise this op to the LLM under the chat-tool
> name `read_mcp_resource`; the OS aliases it back to the `mcp_read_resource`
> kind at the parse boundary — same pattern as `mcp`/`call_mcp_tool`.

The OS resolves the server's transport, dispatches via `MCPClient.read_resource` (gated on the server's negotiated `resources` capability — see `require_capability` in `mcp/client.py`), and returns `{"contents": [...]}`. Every call emits `mcp_resource_read`, `mcp_resource_read_completed`, and (on failure) `mcp_resource_read_failed` events.

**Discovery is NOT gated.** `list_mcp_resources` / `list_mcp_resource_templates` (the chat-tool names for `MCPClient.list_resources` / `list_resource_templates`) mirror `list_mcp_tools`: no `control-ir` op kind, no permission gate — pure discovery, routed directly through `MCPGateway` from the router host adapter. Only the content-returning read is a gated op kind, matching the existing `mcp` (call_tool) vs. discovery (`list_tools`) split.

`resources/subscribe` + `resources/updated` push notifications are `mcp_subscribe_resource` / `mcp_unsubscribe_resource` below (#2597 slice ②b).

## `mcp_subscribe_resource` / `mcp_unsubscribe_resource`

Subscribe to (or cancel a subscription to) server-pushed `notifications/resources/updated` for one resource URI on a configured MCP server. #2597 slice ②b — the async push event-source: MCP's `resources/subscribe` is a **state-sync/watch** mechanism, not a message queue — the server pushes a thin "this URI changed" signal (no payload), and the OS re-reads (`mcp_read_resource` / `read_mcp_resource`) to see the new content.

```json
{"kind": "mcp_subscribe_resource", "server": "filesystem", "uri": "file:///README.md"}
```

```json
{"kind": "mcp_unsubscribe_resource", "server": "filesystem", "uri": "file:///README.md"}
```

Fields (both kinds): `server` (required), `uri` (required — a resource URI as advertised by `resources/list`).

Gated by the **same** `permissions.mcp` axis as `mcp` / `mcp_read_resource` (subscribing is a stateful action against the server). Gated ALSO on the server's negotiated `resources.subscribe` sub-capability — distinct from the coarser `resources` capability `mcp_read_resource` gates on: a server may support reading resources without supporting subscriptions to them (`MCPClient.subscribe_resource` fails fast with `MCPCapabilityError` if the server didn't advertise `resources.subscribe=True` at connect).

**Persistent connection required.** A subscription is only meaningful on a HELD (session-lifetime) MCP connection — the subscribed-URI set is tracked in-memory on `MCPConnectionService` (runtime-only, no WAL: a subscription carries no data of its own, so it is fully re-establishable and matches the gen-store runtime-only-state invariant). An ephemeral session (whose per-call `MCPClientPool` closes the connection immediately after the op returns) refuses both ops with a clear error rather than silently accept a subscription that can never observe a push.

**Reconnect re-subscribes automatically.** A transport-death reconnect (the same F1 healing path `mcp`/`mcp_read_resource` use) opens a fresh `mcp.Client` (#3698 PR-1 — was a raw `mcp.ClientSession`; `Client` now owns building and entering that session internally), which starts with no subscriptions of its own — `MCPConnectionService` re-issues `subscribe_resource` for every URI still tracked for that server immediately after the fresh connection opens, so a subscription survives a dropped transport transparently.

**The push notification itself is an EventLog event, not a `control_ir_results` value.** When the server sends `notifications/resources/updated {uri}`, `reyn.mcp.message_handler.ReynMCPMessageHandler.on_resource_updated` emits an `mcp_resource_updated` event (`server`, `uri`) onto the session's `EventLog` — asynchronously, independent of any op call. This slice deliberately stops at the EventLog: wiring `mcp_resource_updated` into the hook dispatcher is a later (hooks-arc) slice. Re-reading subscribed resources on reconnect to catch updates missed while disconnected (a resync-READ, distinct from the re-**subscribe** above) is also a follow-up, not this slice.

Advertised to the LLM under the chat-tool names `subscribe_mcp_resource` / `unsubscribe_mcp_resource` — same alias pattern as `mcp`/`call_mcp_tool`.

**Reading back subscription state (#4686).** `list_mcp_subscriptions` (no args, no `control-ir` op kind — same "discovery is not gated" class as `list_mcp_servers`) reports the current tracked-subscription set, one entry per HELD connection with at least one subscribed URI: `{server, mode, uris, unhonored}`. Unlike every other `mcp_list_*` here, it never reaches `MCPGateway` at all — subscription tracking is entirely session-local state on `MCPConnectionService` (never WAL'd, per the "runtime-only" note above), so `RouterHostAdapter.mcp_list_subscriptions` reads `MCPConnectionService.subscription_summary()` directly. `uris` is deliberately the REQUESTED set, not the honored-only set — a URI the server declined (named in `unhonored`) stays visible rather than disappearing, so a subscription that silently stopped delivering pushes is still discoverable instead of looking identical to one that never existed. `unhonored` is `None` when the connection can't report honored-ness at all (every Legacy `resources/subscribe` connection today — the pre-2026-07-28 protocol has no per-URI ack; a Listen connection can report it). The TUI's MCP pane (`chrome.py`'s `mcp` pane) renders the SAME `subscription_summary()` output as indented URI rows under each server row — one producer, two consumers, so the operator-visible and LLM-visible pictures cannot drift apart.

## `mcp_get_prompt`

Fetches one rendered prompt (its messages) from a configured MCP server. #2597 slice ②c (prompts consumption) — gated by the **same** `permissions.mcp` axis as `mcp` (call_tool) / `mcp_read_resource`: a rendered prompt returns external, potentially sensitive server-authored content, so it is permission-gated identically.

```json
{
  "kind": "mcp_get_prompt",
  "server": "filesystem",
  "name": "summarize",
  "arguments": {"style": "brief"}
}
```

Fields: `server` (required — must match a key under `mcp.servers:` in `reyn.yaml`), `name` (required — prompt name as advertised by the server's `prompts/list` response), `arguments` (optional, default `{}` — rendering arguments matching the prompt's declared `arguments` schema).

> **Advertised name.** Phases advertise this op to the LLM under the chat-tool
> name `get_mcp_prompt`; the OS aliases it back to the `mcp_get_prompt` kind
> at the parse boundary — same pattern as `mcp`/`call_mcp_tool` and
> `mcp_read_resource`/`read_mcp_resource`.

The OS resolves the server's transport, dispatches via `MCPClient.get_prompt` (gated on the server's negotiated `prompts` capability — see `require_capability` in `mcp/client.py`), and returns `{"description": str | None, "messages": [...]}` — each message a flattened `PromptMessage` (`role` + `content`). Every call emits `mcp_prompt_get`, `mcp_prompt_get_completed`, and (on failure) `mcp_prompt_get_failed` events.

**Discovery is NOT gated.** `list_mcp_prompts` (the chat-tool name for `MCPClient.list_prompts`) mirrors `list_mcp_resources`/`list_mcp_tools`: no `control-ir` op kind, no permission gate — pure discovery, routed directly through `MCPGateway` from the router host adapter. Only the content-returning get is a gated op kind, matching the existing `mcp`/`mcp_read_resource` vs. discovery split.

**Prompts have no subscribe concept.** Unlike resources (`mcp_subscribe_resource`/`mcp_unsubscribe_resource`), MCP's `prompts` capability has no server-push notification for a specific prompt's content changing — only the coarser `notifications/prompts/list_changed` (bridged to an EventLog event by `reyn.mcp.message_handler.ReynMCPMessageHandler.on_prompt_list_changed`, independent of this op kind). There is no `mcp_subscribe_prompt` to build.

## MCP dispatch: discovery lives on the adapter, content/action ops go through `execute_op` (#3409/#3411/#3447)

Tool handlers in `src/reyn/tools/*.py` reach MCP capability by two different routes:

- **Discovery** (`list_mcp_servers`, `list_mcp_tools`, `list_mcp_resources`, `list_mcp_resource_templates`, `list_mcp_prompts`) — a handler calls `host.mcp_list_*(...)`, where `host` is `RouterHostAdapter` and these are the adapter's OWN methods (#3447 folded them off `Session`, which used to inject each as a constructor-callback — see history below). They route straight through `MCPGateway`, bypassing `execute_op` entirely — these five are declared "no permission gate, no op-kind" in `schemas/models.py` itself (the same file `OP_KIND_MODEL_MAP` lives in): discovering *what's available* is not gated, only reading/calling content is (see "Discovery is NOT gated" above and at `mcp_read_resource`). `list_mcp_subscriptions` (#4686) is the same "no permission gate, no op-kind" class but does NOT route through `MCPGateway` — it reads `MCPConnectionService.subscription_summary()` directly (session-local state, no server round trip), so it is listed here separately rather than folded into the five above.
- **Content/action** — a handler calls `execute_op(TypedIROp, ctx)` directly. The five op kinds documented above — `mcp` (call_tool), `mcp_read_resource`, `mcp_subscribe_resource`, `mcp_unsubscribe_resource`, `mcp_get_prompt` — all go through this path.

### The error contract: raise at the gateway seam, catch at the tool-handler boundary

Both routes now share the same exception discipline: `Cancelled`/`MCPFault` propagate as exceptions out of the `MCPGateway` call and are **not** caught inside `RouterHostAdapter`. The catch happens at the tool-handler boundary in `src/reyn/tools/mcp.py`:

- The 5 content/action op kinds never catch at all — an uncaught exception reaches `dispatch_tool`, which promotes it to the outer envelope (`{status: "error", error: {kind, message}}`, #3450/#3478).
- The 5 discovery methods route every gateway call through `_call_mcp_list` (`tools/mcp.py`), a shared try/except that reproduces the exact `{"error": "cancelled"}` / `{"error": str(exc)}` shape the caller received when the catch lived inside `Session._mcp_list_via_gateway` (pre-#3447). A separate failure mode — an unresolved server config (roster empty / server not configured / config not a dict) — is NOT an exception at all; `RouterHostAdapter._mcp_resolve_server_config` returns it as the same `[{"error": ...}]` sentinel shape as an early return, and `_mcp_list_error` (also `tools/mcp.py`) detects it identically whether it came from config resolution or from `_call_mcp_list`'s catch.

Architect firm (#3411, 2026-07-29): moving the catch upward is behavior-preserving, not a contract change — no context-manager / audit-emit / pool-teardown step sits between the raise site (inside `MCPGateway`) and either catch position (the old in-`Session` one or the new in-`tools/mcp.py` one).

**Out of scope, by explicit negative acceptance criterion (#3447):** the call-family op kinds (`mcp`, `mcp_read_resource`, `mcp_get_prompt`) were ALREADY unified on the throw contract before this fold and get NO new catch — adding one would change their LLM-visible failure shape (today: `dispatch_tool`'s `Error (kind): message` envelope; a handler-level catch would instead surface as an ok-shaped `{"error": ...}` dict, a real behavior change). "Symmetric with discovery" is not a reason to add one; see #3447 (architect self-correction) for the full reasoning against a shared call-family catch helper.

### History: why this used to be two structurally different paths (#3409)

Before #3447, discovery routed through `Session` callbacks injected into `RouterHostAdapter.__init__` (`mcp_list_servers=self._mcp_list_servers`, etc. — five separate constructor parameters), which is why `RouterHostAdapter.__init__` carried 10 `mcp_*` callback parameters instead of one facade or zero. The five discovery methods shared one seam, `Session._mcp_list_via_gateway`, which — unlike the content/action ops — caught `Cancelled`/`MCPFault` itself and returned `[{"error": ...}]` rather than raising (#3409's "the actual blocker: the error contract, not permission" finding). #3411 settled the error-contract question (raise, matching the content/action ops) once #3443 gave the discovery handlers a shared catch point (`_mcp_list_error`) to normalize the raised exception back into the same wire shape. #3447 did the resulting structural move: the five discovery methods became `RouterHostAdapter`'s own methods (no more `Session`-side implementation, no more constructor callback for these five — the adapter already duplicated the two inputs they needed, `_mcp_servers_flat`/`_get_mcp_servers_for_router`), and the exception catch moved from `Session._mcp_list_via_gateway` to `tools/mcp.py`'s `_call_mcp_list`.

## `mcp_install`

Installs an MCP server from `registry.modelcontextprotocol.io` into the project's config.
**Phase-only** (not available from the router). Requires `permissions.mcp_install: true`
in the skill's frontmatter **and** user approval.

```json
{
  "kind": "mcp_install",
  "server_id": "io.github.modelcontextprotocol/server-filesystem",
  "scope": "local",
  "env_overrides": {"GITHUB_TOKEN": "ghp_..."}
}
```

Fields:
- `server_id` (required — required even when `source` is set, since it feeds the
  audit event; when installing from a `source`, derive it from the specifier or
  pass an empty string) — registry identifier (e.g. `"io.github.foo/bar-mcp"`).
- `scope` (optional, default `"local"`) — config tier to write to:
  - `"local"` → `<project>/.reyn/config.yaml`
  - `"project"` → `<project>/reyn.yaml`
  - `"user"` → `~/.reyn/config.yaml`
- `env_overrides` (optional) — pre-supplied secret env values; skip interactive prompt
  for keys present here.
- `source` (optional) — a `--source` specifier that, when set, SKIPS the
  registry fetch entirely and resolves server metadata directly from the
  specifier instead: `"npm:@scope/pkg"`, `"pypi:my-mcp-server"`,
  `"docker:my-org/server"`, or a GitHub URL. The resolved server name (from
  the specifier) becomes the config key, falling back to `server_id`'s short
  name when resolution doesn't name one.
- `extra_args` (optional, list[str]) — extra CLI args appended to the
  resolved server's `args` list after registry/source resolution — for a
  server that needs a runtime flag beyond what the registry/source metadata
  supplies (e.g. `["--server", "pyright"]`).

Handler lifecycle:
1. Fetches `server.json` via `RegistryClient`
2. Checks runtime command availability (`npx` / `uvx` / `docker` / `dnx`)
3. Gates via `PermissionResolver.require_file_write` (= `.reyn/config/mcp.yaml`) + `require_http_get` (= registry host); the legacy `require_mcp_install` bool-axis gate has been removed
4. Prompts for `isSecret=true` env vars via `intervention_bus`; each `save_secret` routes through `PermissionResolver.require_secret_write` (= Phase 6 wildcard `"*"` covers the runtime-determined key set)
5. Writes `mcp.servers.<name>` to the target scope config file
6. Emits `mcp_server_installed` event (P6) — key names only, no values

> **`index_write` removed (still); `embed` re-exposed (FP-0057 Phase 1); `index_update`
> added (FP-0057 Phase 2a) and its safe-mode entry point retired `embed_and_index`
> clean-break (FP-0057 Phase 2b), and that safe-mode entry point itself later
> retired clean-break (FP-0066 P1c).** The `index_write` control-IR op stays
> removed. `index_update` used to be reachable from a safe-mode `python` step
> via `reyn.api.safe.index_update()` — a thin dispatch onto the `index_update`
> op (incremental/delta-reconcile: add/update/remove/skip against the source's
> current index), which itself calls the shared `embed` op (via `execute_op`, not
> provider-direct) for the actual embedding. The old `reyn.api.safe.embed_index.
> embed_and_index()` (provider-direct, append/replace) was **deleted, no shim**
> in FP-0057 Phase 2b — the bundled `index_docs` / `index_events` chunkers were
> removed earlier along with the stdlib skills that wrapped them, unaffected by
> that retirement. **FP-0066 P1c then retired `reyn.api.safe.index_update()`
> itself, clean-break, plus the CLI `reyn source` command group** — the in-core
> index is OS-internal only now (populated by internal `index_update` op
> callers, not a user-facing entry point); user RAG is the FP-0063 plugin over
> an external store. `semantic_search` (FP-0057 Phase 2a; renamed from `recall`)
> also embeds its
> query via the shared `embed` op (not provider-direct — the query-embed path was
> switched onto `execute_op(EmbedIROp(...))` so it passes the same PRE-embed
> redaction-egress seam as ingestion), per-source-model. The `EmbeddingProvider`
> and `SqliteIndexBackend` primitives are unchanged. The `embed` op, however, is
> **no longer removed**: FP-0057 Phase 1 re-added it as the user-facing raw
> embedding primitive (see the [`embed`](#embed) section above) — #1303's "no
> caller" rationale for removing it is obsolete now that the primitive is exposed
> for user RAG composition AND is the shared internal mechanism `index_update` /
> `semantic_search` dispatch through. So `kind: embed` is emitted again; only
> `kind: index_write` is not.

## `mcp_drop_server`

Removes an MCP server from project/local/user config — the counter-op to
`mcp_install` (FP-0034 §D23). Purely mechanical (no LLM reasoning needed);
lives in the universal catalog under `mcp.operation__drop_server`. Requires
`permissions.mcp_drop_server: true` in the skill's frontmatter — a
DISTINCT decl field from `mcp_install`'s, so install intent alone never
implies drop intent (prevents an install-only agent from accidentally
tearing down user-configured servers).

```json
{
  "kind": "mcp_drop_server",
  "server": "filesystem",
  "clear_secrets": true
}
```

Fields:
- `server` (required) — the short config key (e.g. `"filesystem"`).
- `scope` (optional, default `None` = auto-detect) — which config tier to
  remove from (`"local"` / `"project"` / `"user"`, same mapping as
  `mcp_install`'s `scope`). When omitted, the handler walks
  dynamic → local → project → user and removes from the first tier that
  contains `server`.
- `clear_secrets` (optional, default `true`) — also remove the server's
  `${KEY}=value` secret entries from `~/.reyn/secrets.env` (keyed by the
  entry's `env` block at removal time). The CLI's own default leaves
  secrets behind for safety; the LLM path defaults to cleaning them up on
  the premise that an LLM's drop intent is more deliberate.

Handler lifecycle:
1. Resolve scope — explicit (`op.scope`) or auto-detect by walking tiers
   for the first that contains `server`; not found anywhere → a structured
   `{status: "not_found"}` result, never an exception (the LLM can
   `list_actions`/retry rather than crash the turn).
2. Gate via `PermissionResolver.require_file_write` over the scope's config
   file — mirrors `mcp_install`'s gate; the old per-server bool-axis
   `require_mcp_drop_server` prompt was removed by the #571
   permission-collapse arc (per-server granularity is now an operator
   config-level concern, not a per-op runtime one).
3. Capture the entry's `env` block's key names (before mutation) so the
   secrets cleanup step in 5 knows what to clear.
4. Remove the entry from the scope's YAML, pruning now-empty `mcp.servers`/
   `mcp` containers so the file stays tidy; `record_config_generation`
   (recovery-core: truncation-surviving snapshot, #2259 / CLAUDE.md gate).
5. When `clear_secrets` is `true`, remove the captured keys via
   `reyn.security.secrets.store.clear_secret` — secret VALUES are never
   read or emitted, only key names.
6. Emits `mcp_server_removed` event (P6 audit trail).

Result fields: `status` (`"ok"` / `"not_found"`), `server`, `scope`,
`removed_path`, `env_keys_cleared`, `secrets_cleared`.

## `skill_install`

Registers a skill (from a local directory or a git/GitHub source URL) into the
project's `skills.entries` config. Two tool surface verbs converge on the same
`op_runtime/skill_install.py` handler: `skill_install_local` (local
path) and `skill_install_source` (git/URL, PR-D, #2548).

Local-path example:
```json
{
  "kind": "skill_install",
  "path": "skills/my-skill",
  "name": "my-skill"
}
```

Source/git example:
```json
{
  "kind": "skill_install",
  "source": "https://github.com/user/skill-repo",
  "name": "my-skill"
}
```

Subdir convention (mirrors Terraform): `"https://github.com/user/repo//skills/my-skill"`
selects the `skills/my-skill` subdirectory inside the cloned repo.

Fields:
- `path` (required when `source` is absent) — path to the skill directory (containing
  `SKILL.md`) or the direct path to the `SKILL.md` file. May be absolute or
  project-root-relative. When pointing at a directory the handler appends `/SKILL.md`.
  Ignored when `source` is set.
- `source` (optional, PR-D) — git or GitHub URL. The handler shallow-clones the repo
  to `.reyn/skills/<name>/`. Subdir inside the repo is specified via `//` separator.
  Requires `http.get: [{host: <source_host>}]` in the caller's permission declaration.
- `scope` (optional, default `".reyn/config/skills.yaml"`) — retained for
  forward compat; currently unused (all installs write to `.reyn/config/skills.yaml`).
- `name` (optional) — config key override. When absent the handler resolves:
  frontmatter `name:` field → directory basename → repo/subdir basename (in that order).
  The resolved name is **sanitized to a single safe path component** (`[A-Za-z0-9._-]`;
  no `/`, `\`, `..`, or leading `.`) — an unsafe name (from caller `op.name` OR third-party
  SKILL.md frontmatter) is **rejected** with `status="error"`, never used to build a path.
- `plugin_id` (optional, ADR 0064 §3.7, plugin model P2) — when set, stamped
  verbatim as `entry["plugin_id"]` on the written `skills.yaml` entry. Set
  ONLY by `plugin_install` when it calls this handler internally to register
  a plugin's `skills/` capability — the additive provenance field
  `plugin_uninstall` reads back to find every entry a given plugin created.
  Absent (`None`) for a direct `skill_install` call, unchanged from before
  this field existed.

Handler lifecycle (source path inserts steps 0a–0d before step 1):
0. **Source path only**: (a) Gate `require_http_get` for the source host. (b) Sanitize the
   candidate name (`_safe_skill_name`) + verify the clone destination is contained under
   `.reyn/skills/` (`_contained_under`) — refuse before any filesystem mutation if either
   fails (path-traversal → arbitrary-rmtree guard). Shallow-clone repo to
   `.reyn/skills/<candidate_name>/`. (c) Locate `SKILL.md` in root or subdir.
   (d) After the frontmatter name is resolved AND sanitized, containment-check + rename
   clone dir if name ≠ candidate.
1. Resolve `SKILL.md` path (dir → `<dir>/SKILL.md` or direct file)
2. Read `SKILL.md` and `split_frontmatter()` — extract `name` and `description`
3. Apply `op.name` override when set
4. Threat-scan description via `content_guard.scan_for_threats(scope="strict")` — block on
   blocking-severity match (source path: removes clone on block)
5. Gate via `PermissionResolver.require_file_write` (= `.reyn/config/skills.yaml`)
6. Write `skills.entries.<name>` to `.reyn/config/skills.yaml` with
   `{path, description, enabled: true, visibility: menu}` (+ `source: <url>` when set)
7. Call `record_config_generation` (recovery-core: truncation-surviving snapshot, #2259 / CLAUDE.md gate)
8. Emit `skill_installed` event (P6 audit trail)
9. Request hot-reload via `get_active_hot_reloader().request_reload(source="skill_install")`

Result fields: `status` (`"installed"` / `"blocked"` / `"error"`), `name`, `path`,
`description`, `config_path`, `source` (empty string for local installs).

Events emitted: `skill_install_threat_match`, `skill_install_threat_blocked` (threat scan),
`skill_installed` (P6 on success).

## `load_skill`

**FP-0066 P0 (#3247).** The dedicated skill-activation verb — loading a
skill's `SKILL.md` body IS the invocation (#2971's rationale still holds:
a skill body is model instructions, not code to execute; there is still no
`run_skill` op). Extracted OUT of `read_file`'s former `is_skill_body_path`
special-case (ADR 0064 §3.5 originally called for a dedicated verb; #2971
instead folded it into the ordinary read op — this reverses that drift).
Tool surface: `load_skill`
(ratified per the #3223 naming-convention arc).

```json
{
  "kind": "load_skill",
  "path": "skills/my-skill/SKILL.md"
}
```

Fields:
- `path` (required) — the skill's `SKILL.md` path, as surfaced by the L1
  `## Skills` menu or the `skill_list` tool.

Handler lifecycle (`op_runtime/load_skill.py`):
1. **Resolve-once (#3196 co-vet round 2, security-critical)**: `op.path` is
   resolved via `reyn.core.op_runtime.context.resolve_path_for_gate`
   EXACTLY ONCE, into `resolved_path`; every later decision (permission
   gate, provenance classification, the actual byte read) reuses THIS SAME
   string. A separate, independent resolve for "is this trusted" vs "what
   do I read" reopens the symlink-swap TOCTOU window #3196 closed.
2. **Builtin/plugin bypass**: `read_builtin_body_bytes` (#2913/#2914) /
   `read_plugin_body_bytes` (`plugin_install.is_registered_plugin_root`) —
   the SAME helpers `file.py` uses for its own (unrelated) general
   builtin/plugin body reads. A non-`None` result already IS a trusted
   provenance (`"builtin"` / `"plugin"`), not merely a permission-bypass
   signal, and skips the ordinary `require_file_read` gate.
3. **Permission gate**: `require_file_read` against `resolved_path` when the
   bypass did not fire.
4. **Read + decode**: `ctx.workspace.read_file_bytes` (or the bypass bytes)
   → the shared text-codec decode ladder. A binary result is an error (a
   skill body must be text).
5. **Provenance classification (#3196)**: builtin/plugin from step 2, or
   `config_entry` when `resolved_path` matches an entry in
   `ctx.available_skills` (the SAME registered-skill snapshot `:skill`
   invocation resolves against). A path matching NONE of the three classes
   is still returned — plain, unexpanded, no event (fails closed).
6. **Expansion**: when provenance is set, `reyn.plugins.skill_load.
   load_skill_body` expands `${REYN_PLUGIN_ROOT}`/`${REYN_SKILL_DIR}`/
   `${REYN_PROJECT_DIR}`/`${CLAUDE_*}` aliases unconditionally, and
   `${env:VAR}` only when `VAR` is declared on `ctx.permission_decl.
   env_expand` (#3198, deny-by-default) — an undeclared or unset name's
   token is left unexpanded, never blanked. **#3629**: this is the fully-
   expanded string returned as `content` (what the model reads THIS
   turn, unchanged); `load_skill_body` ALSO returns a second, persist-safe
   variant with `${REYN_SKILL_DIR}`/`${REYN_PLUGIN_ROOT}` (+ `${CLAUDE_*}`
   aliases) left LITERAL, plus the location-token map, surfaced as this
   op result's `content_history`/`token_map`/`skill_source_path` fields —
   see step 9 below for why.
7. **Threat-scan (#4699, the ratified band)**: `content_guard.scan_for_threats`
   (`scope="strict"`, same shape `skill_install`'s description scan uses) runs
   on the final `content` — the ONE chokepoint every skill body crosses
   regardless of how it was registered (a hand-written `skills.yaml` entry
   never passes through `skill_install`'s own scan). A blocking-severity
   match returns `status: "blocked"` with `content: ""` — the body never
   reaches this turn's context. Mirrors the existing `scan_tool_result`/
   `fence_tool_result` tool-result chokepoint, not a new mechanism.
8. **Self-bounding**: an oversized body is truncated to the resolver's
   inline cap (`control_ir_inline_cap`) rather than blowing the context;
   `status: "truncated"` + a `note` on that path.
9. **#3629 — persist-safe history, not the expanded value**: `content` is
   what the current turn's LLM call sees; `content_history` (present only
   alongside a set provenance) is what `router_loop.py`'s tool-result
   assembly persists to `history.jsonl` instead — history is immutable, so
   baking an absolute path into it froze a value that a later rename/move
   (#3588 was one instance) could turn stale forever, with no way for the
   model to tell a stale absolute path from a live one. A wire-serialise
   pass (`RouterHistoryBuffer._serialise_turn` →
   `reyn.plugins.skill_load.refresh_location_tokens`) re-resolves the
   literal tokens fresh, against the CURRENT filesystem, every time a
   persisted entry is replayed — `token_map` is audit-completeness
   metadata only (never a re-expansion source).

Result fields: `status` (`"ok"` / `"truncated"` / `"blocked"` / `"not_found"` /
`"error"`), `path`, `content` (empty on `"blocked"`), plus
`total_chars`/`_truncated`/`note` on a truncated result, `encoding` when a
non-UTF-8 codec was used, and (#3629) `content_history`/`token_map`/
`skill_source_path` when a provenance class matched (step 6).

Events emitted: `tool_executed` (`op="load_skill"`) always; `skill_body_threat_match`
/ `skill_body_threat_blocked` (#4699, threat scan, step 7) when the body
matches; `skill_body_loaded`
(`provenance`, `env_tokens_expanded`/`env_names_expanded`,
`env_tokens_denied`/`env_names_denied` — names + counts only, NEVER the
expanded/denied values) only when provenance was classified.

## `pipeline_install`

Registers a pipeline (from a local DSL file or a git/GitHub source URL) into the
project's `pipelines.entries` config. Two tool surface verbs converge on the same
`op_runtime/pipeline_install.py` handler: `pipeline_install_local` (local
path) and `pipeline_install_source` (git/URL). Mirrors `skill_install`
as closely as possible, reusing its generic path-safety + sandboxed git-clone helpers
verbatim (`_safe_skill_name` / `_contained_under` / `_parse_source_spec` /
`_source_host` / `_shallow_clone` / `_read_yaml` / `_write_yaml` /
`_resolve_project_root` carry no skill-specific logic).

Local-path example:
```json
{
  "kind": "pipeline_install",
  "path": "pipelines/hello.yaml"
}
```

Source/git example:
```json
{
  "kind": "pipeline_install",
  "source": "https://github.com/user/pipeline-repo"
}
```

Subdir convention (mirrors Terraform, same as `skill_install`):
`"https://github.com/user/repo//pipelines/my-pipeline"` selects the
`pipelines/my-pipeline` subdirectory inside the cloned repo.

Fields:
- `path` (required when `source` is absent) — the direct path to the pipeline's
  `*.yaml` DSL file. Unlike `skill_install`, there is no directory-or-file
  resolution — a pipeline registration is always exactly one file. For a source
  install, `path` (when set) selects the DSL file relative to the repo root/subdir;
  when omitted, the repo root/subdir must contain exactly one `*.yaml` file.
- `source` (optional) — git or GitHub URL. The handler shallow-clones the repo
  to `.reyn/pipelines/<name>/`. Subdir inside the repo is specified via `//` separator.
  Requires `http.get: [{host: <source_host>}]` in the caller's permission declaration.
- `scope` (optional, default `".reyn/config/pipelines.yaml"`) — retained for
  forward compat; currently unused (all installs write to `.reyn/config/pipelines.yaml`).
- `name` (optional, #2722) — a free NAMESPACE KEY, NOT coupled to any declared
  `pipeline:` name. Every `pipeline:` document in the file registers under
  `{name}.{declared-name}`; `.` is reserved (the namespace separator) and
  rejected in `name`. When omitted, the key defaults to the DSL file stem
  (or the source basename for a git install).
- `plugin_id` (optional, ADR 0064 §3.7, plugin model P2) — mirrors
  `skill_install`'s `plugin_id` field verbatim (same stamp-on-entry
  mechanism, set only by `plugin_install`'s internal call).

Handler lifecycle (source path inserts steps 0a–0d before step 1):
0. **Source path only**: (a) Gate `require_http_get` for the source host. (b) Sanitize the
   candidate name + verify the clone destination is contained under
   `.reyn/pipelines/` — refuse before any filesystem mutation if either
   fails (path-traversal → arbitrary-rmtree guard). Shallow-clone repo to
   `.reyn/pipelines/<candidate_name>/`. (c) Locate the DSL file (`path` selects it, or
   the sole `*.yaml` file in the repo root/subdir). (d) After the declared name is
   resolved AND sanitized, containment-check + rename clone dir if name ≠ candidate.
1. Resolve the DSL file path (local: `op.path` directly; source: the located clone file)
2. Parse via `parse_pipeline_docs` — a file may hold MULTIPLE `pipeline:` documents
   (#2722); a malformed file is refused (`status="error"`), never registered
3. Resolve the registration namespace key (#2722): `op.name` or the DSL file stem
   (source install: the sanitized candidate derived pre-clone); `.` is rejected
4. Threat-scan EVERY pipeline document's description via
   `content_guard.scan_for_threats(scope="strict")` — block on any blocking-severity
   match (source path: removes clone on block)
5. Gate via `PermissionResolver.require_file_write` (= `.reyn/config/pipelines.yaml`)
6. Write `pipelines.entries.<name>` to `.reyn/config/pipelines.yaml` with
   `{path, description, enabled: true}` (+ `source: <url>` / `plugin_id: <id>` when set)
7. Call `record_config_generation` (recovery-core: truncation-surviving snapshot, #2259 / CLAUDE.md gate)
8. Emit `pipeline_installed` event (P6 audit trail) — carries `registered_names`,
   the FULL set of `{key}.{declared-name}` global names this install registers (#2722 H6)
9. Request hot-reload via `get_active_hot_reloader().request_reload(source="pipeline_install")`
   (the existing `"pipelines"` seam — `Session._reapply_pipelines` — rebuilds the registry)

Result fields: `status` (`"installed"` / `"blocked"` / `"error"`), `name`,
`registered_names`, `path`, `description`, `config_path`, `source` (empty string for
local installs).

Events emitted: `pipeline_install_threat_match`, `pipeline_install_threat_blocked` (threat scan),
`pipeline_installed` (P6 on success).

## `presentation_install`

Registers a named presentation template (a declarative component tree) into the
project's `presentations.entries` config (proposal 0060 Phase 1 Layer A, A8).
One tool surface verb: `presentation_install_local`, handled by
`op_runtime/presentation_install.py`. Mirrors `skill_install` /
`pipeline_install`'s STRUCTURE (permission gate → config write →
`record_config_generation` → emit event → hot-reload), but there is **no**
source/git-fetch path (a blueprint is small declarative data carried inline,
never a file-backed artifact) and **no** `scan_for_threats` call — a present
blueprint is structurally non-executable by construction
(`reyn.core.present.catalog`: 8 fixed components, every non-literal value is a
`$bind` RFC-6901 JSON-Pointer, no template-ref/eval/exec surface, `image.src`
renders as a label — no fetch/SSRF); `validate_blueprint` (the SAME gate an
inline `present(blueprint=...)` op already passes through) fills the role
`scan_for_threats` fills for skill/pipeline free-text `description`.

Example:
```json
{
  "kind": "presentation_install",
  "name": "status_card",
  "blueprint": {
    "component": "keyvalue",
    "rows": [{"label": "status", "value": {"$bind": "/status"}}]
  }
}
```

Fields:
- `name` (required) — the `presentations.entries` config key; the value a
  `present(view=<name>)` op resolves against.
- `blueprint` (required) — the declarative component tree, identical shape to
  an inline `present(blueprint=...)`'s `blueprint` field.

Handler lifecycle:
1. Structural threat gate: `validate_blueprint(op.blueprint)` — refuses
   (`status="blocked"`) BEFORE any config mutation on a malformed / non-catalog
   blueprint.
2. Gate via `PermissionResolver.require_file_write` (= `.reyn/config/presentations.yaml`)
3. Write `presentations.entries.<name>` to `.reyn/config/presentations.yaml`
   with `{blueprint, enabled: true, provenance: <ctx.turn_origin>}` —
   `provenance` is OS-stamped from `ctx.turn_origin` alone (A7/A9), never from
   an op field
4. Call `record_config_generation` (inherits the existing config crash-recovery;
   no new recovery-gated obligation — no truncate-falsify test owed for this op)
5. Emit `presentation_installed` event (P6 audit trail)
6. Request hot-reload via `dispatch_install_reload(source="presentation_install")`
   (the existing `"presentations"` seam — `Session._reapply_presentations`,
   FP-0054 PR-C — rebuilds the registry; the SAME seam operator edits to
   `presentations.yaml` already reload through)

Ships inert-by-construction: a presentation is invoke-by-name — it renders
only when a `present(view=<name>)` op names it, so a freshly-installed
template is discoverable but dormant until referenced (no new state needed,
mirrors builtin-inert for skills/pipelines).

Result fields: `status` (`"installed"` / `"blocked"` / `"error"`), `name`,
`config_path`.

Events emitted: `presentation_install_blocked` (structural gate),
`presentation_installed` (P6 on success).

## `plugin_install` / `plugin_uninstall`

ADR 0064 (plugin model) P2 install machinery. A plugin is a self-contained
directory (`plugin.json` manifest at the plugin root + optional `mcp`/`pipelines`/
`skills` subdirs, ADR §3.1, relocated from `.reyn-plugin/plugin.json` — #4570
conversion A, aligning with the Agent Plugins 1.0 standard's manifest
location) — `plugin_install` copies it to
`~/.reyn/plugins/<name>/` (global, once), bakes stable-location tokens into
the copy per file kind (step 6 below — `${REYN_*}` for `pipelines/*.yaml`/
`SKILL.md`, the standard's own `${PLUGIN_ROOT}`/`${PLUGIN_DATA}` for
`mcp.json`), and REGISTERS whatever capability kinds the plugin directory
contains (`mcp`/`pipelines`/`skills`, derived PURELY from directory/file
existence via `capability_kinds_present` — #4570 conversion B removed the
manifest's own `capabilities`/`entries` fields, so this is no longer a
manifest declaration) by calling the SAME existing verbs `skill_install` /
`pipeline_install` already provide (plus a direct `.reyn/config/mcp.yaml`
write for the optional root `mcp.json`) — an orchestration layer, not a
fourth registry.

**Register-only** (#3209 — architect-firm redesign, owner GO 2026-07-23):
`plugin_install` never provisions a plugin's external Python dependencies.
The pre-#3209 design materialised a per-plugin venv (`<sys.executable> -m
venv` + `pip install`) at install time and rewrote a `command: "python"` mcp
entry to that venv's interpreter — a foreign env-provisioning responsibility
riding a registration op. That entire step (its two interpreter-path
resolvers, the venv materialise call, the `_deps_materialised` install-state
stage) is REMOVED, clean-break, no transition shim. A plugin's
`requirements.txt` (if present) is now inert data plugin_install copies but
never reads: external deps are **skill-driven** — the installing skill's
SETUP instructions walk the operator/LLM through creating their OWN venv,
`pip install -r requirements.txt` inside it, and pointing the plugin's
`mcp.json` server `command` at that venv's python interpreter absolute
path directly (Windows: `Scripts\python.exe`). `plugin_install` registers
whatever `command` the plugin's `mcp.json` names AS-IS — no rewrite of any
kind. **Fail-fast preserved** (#3060 by-construction requirement): a
`command` naming an incomplete/missing venv fails at MCP spawn with a clear
OS-level error; plugin_install/spawn never falls back to a runtime fetch.
See ADR 0064 §3.11a for the interpreter-path-resolution history this
redesign supersedes. Handled by
`op_runtime/plugin_install.py` / `op_runtime/plugin_uninstall.py`. LLM tool
surface: `install_plugin` / `uninstall_plugin`
(`tools/plugin_management_verbs.py`) — named distinctly from the op kind to
avoid a canonical-declaration collision (mirrors the `mcp_install_local` vs
`mcp_install` op-kind precedent).

ADR §3.9 (P3): the SAME typed op is also exposed as a slash command
(`/plugin install builtin|local|git <SOURCE> [as <INSTALL_NAME>]` /
`/plugin uninstall <NAME>`, `interfaces/slash/plugin.py`) and a CLI command
(`reyn plugin install builtin|local|git <SOURCE>` / `reyn plugin uninstall
<NAME>`, `interfaces/cli/commands/plugin.py`) — both thin adapters that build
a `ToolContext` and call `invoke_tool(get_default_registry(),
"install_plugin"/"__uninstall", ...)`, the SAME lookup+dispatch a
live chat-router LLM tool call uses. No surface re-implements the security
logic: the composite permission decl is declared once in
`tools/plugin_management_verbs.py` (the tool wrapper), and the `{kind: "git"}`
run-code trust gate itself (below) lives one layer down in
`core/op_runtime/plugin_install.py::handle` — the op handler every surface
funnels into. The slash surface threads the session's LIVE `RouterHostAdapter.
make_router_op_context` (real intervention bus - a `{kind: "git"}` install
prompts interactively, and the OpContext carries the `#1339` sandbox floor -
`resolve_sandbox_policy`, write_paths default-restricted to the workspace -
so installing a `{kind: "local"}`/`{kind: "git"}` plugin from `/plugin`
additionally needs an operator `reyn.yaml` `sandbox.policy.write_paths` grant
covering `~/.reyn/plugins/`, same as a live LLM tool call would). The CLI
surface instead builds a standalone `OpContext` directly (no
`build_router_op_context`, no sandbox floor - mirrors `reyn mcp install`'s
CLI-is-the-operator-trusted-entry-point precedent) whose `interactive` flag is
`not --non-interactive and sys.stdin.isatty()`. Either surface, a
non-interactive caller (no intervention bus) fails the `{kind: "git"}`
run-code trust gate closed - that gate is unconditional deny-else
(`require_plugin_git_run_code_trust`), independent of the sandbox floor.

The CLI's floor-bypass is safe by construction because LLM reach into
`~/.reyn/plugins/` is closed at TWO layers, and the CLI is only reachable by
the operator (not the LLM): (1) the OpContext-layer gate — on any LLM-reachable
path (tool/slash) the `#1339` sandbox floor + `require_file_write` deny a write
to `~/.reyn/plugins/` without an explicit operator grant; and (2) the OS-layer —
even an LLM `exec` that tries to write there directly is denied by the sandbox
backend, because the enforced exec policy's `write_paths` is workspace-tight
(`resolve_sandbox_policy` floor = `[workspace.base_dir]`, operator-wins over
LLM op fields per #1326) and Landlock/Seatbelt deny-by-default any write outside
`write_paths` — and `~/.reyn/plugins/` (under `$HOME`) is never under the
workspace grant. So the operator-only CLI skipping the OpContext-layer floor
removes nothing the LLM could have reached anyway.

`plugin_install` example (typed discriminated `source`, §3.8 — never a
form-sniffed string):
```json
{
  "kind": "plugin_install",
  "source": {"kind": "local", "path": "/path/to/my-plugin"},
  "name": "my-plugin"
}
```

`source` is exactly one of:
- `{kind: "builtin", name: "<name>"}` — reyn's own shipped plugin under
  `src/reyn/builtin/plugins/<name>/`. Lowest RCE trust risk.
- `{kind: "local", path: "<dir>"}` — a local directory the LLM authored/tested
  (ADR §3.2's primary daily "promote" loop) or a hand-written plugin already
  on disk. Middle RCE trust risk.
- `{kind: "git", url: "<url>"}` — a remote git URL, shallow-cloned. Highest
  RCE trust risk — gated by a DISTINCT per-install run-code trust decision
  (`require_plugin_git_run_code_trust`, gate 2 below), separate from the fetch
  axis; fetching and running remote code is an explicit operator-trust
  decision, never auto-run and never pre-grantable.

Fields (`plugin_install`):
- `source` (required) — the discriminated union above.
- `name` (optional) — overrides the manifest's own `name` as the
  install-directory / registry-provenance key.

Fields (`plugin_uninstall`):
- `name` (required) — the plugin's install name.

Permission gates (§3.10 — composed from EXISTING gates, no new bool axis; the
#571 collapse arc removed the old bool-axis pattern):
1. **Global-copy write** — `require_file_write` for `~/.reyn/plugins/<name>/`.
   This path is OUTSIDE the default write zone (`.reyn/` under CWD), so the
   existing gate's "zone OR approved" decl-less rule already denies it
   without an explicit approval / JIT ask — no new gate needed.
2. **`{kind: "git"}` run-code trust** — a DEDICATED
   `require_plugin_git_run_code_trust` gate, checked BEFORE the fetch. This is
   the RCE trust boundary and is deliberately SEPARATE from `require_http_get`
   (the fetch axis): fetching bytes and RUNNING them are different decisions.
   `require_http_get` is per-host, PERSISTENT (ALWAYS → `approvals.yaml`), and
   SHARED with `web.fetch`, so a host approved once for a web fetch must NOT
   thereby authorise installing + running its plugin code — else that host
   becomes a standing silent-RCE grant for every future git plugin. The
   run-code gate consults/writes NO approvals map (no key, no ALWAYS path, no
   `reyn.yaml` pre-grant); its choice set (`plugin_run_code_trust_choices`)
   offers only yes/no, so it re-asks EVERY install and can never be
   pre-granted (§3.10 "never auto-run"). Fail-closed: a non-interactive caller
   denies. `require_http_get` for the clone host still runs afterwards
   (defense-in-depth network reachability), but the run-code gate is the one
   that makes `{kind: "git"}` safe.

Name-collision precedence (§3.8/§3.10): when `~/.reyn/plugins/<name>/`
already holds a DIFFERENT-kind completed install, `reyn.plugins.source.
resolve_name_collision` decides the winner (`builtin <= local << git`) — a
lower-trust source is refused (`status="skipped"`), never silently shadows a
higher-trust one.

`plugin_install` handler lifecycle (one-shot):
0. Reconcile: any `~/.reyn/plugins/<name>/` left with an
   `.reyn-plugin/_install_state.json` marker from a PRIOR crashed/interrupted
   install is rolled back before this install proceeds (`reconcile_plugin_installs`,
   §3.11 — self-healing on the next `plugin_install` call; this repo has no
   general process-startup hook, so "next use" is the documented reconcile
   trigger). Rollback mirrors uninstall's **drop-registry-first** ordering: a
   partial that crashed AFTER registering some capabilities left registry
   entries tagged with its `plugin_id` pointing at the dir about to be deleted,
   so those entries are dropped from all three `.reyn/config/*.yaml` registries
   (ungated — OS-internal repair of already-broken entries) BEFORE the copy is
   removed, or a dangling registry entry would survive. Emit
   `plugin_install_reconciled` (`name`, `action` — `"rolled_back"` today, the
   only value emitted) per plugin actually rolled back.
1. Resolve `source` → a source directory per its `kind`, applying the source's
   gate(s): `{kind: "git"}` runs the run-code trust gate (2) then
   `require_http_get` before cloning; `builtin`/`local` touch no network.
2. Load + validate `plugin.json` (plugin root) via `reyn.plugins.manifest.
   load_plugin_manifest` (P1) — a missing/malformed manifest refuses
   (`status="error"`) BEFORE any copy.
3. Name-collision precedence check (above).
4. Gate 1 (global-copy write).
5. Copy: write the `_install_state.json` marker, THEN copy the source tree
   (VCS metadata excluded) into `~/.reyn/plugins/<name>/`. Emit
   `plugin_install_copied`.
6. Bake stable-location tokens into the copy's text files, per file kind
   (`_expand_plugin_files`) — three DIFFERENT bakes, not one shared pass:
   - `mcp.json`: `${PLUGIN_ROOT}`/`${PLUGIN_DATA}` — the Agent Plugins 1.0
     `mcp.schema.json`'s OWN token vocabulary, NOT `${REYN_PLUGIN_ROOT}` (#4570
     conversion D, `_bake_mcp_json_fields`) — FIELD-AWARE: expanded only in
     `args`/`env` values/`cwd`, NEVER in `command` or `url` (the injection
     boundary the standard itself draws — a plugin-authored string must not
     be able to choose WHAT gets executed or WHERE a request goes via a
     token this module resolves). Parsed as JSON, not text, so a token
     appearing inside `command`/`url` is left LITERAL rather than swept up
     by a coincidental string match. `${PLUGIN_DATA}` resolves to
     `plugin_data_root()/<name>` — `~/.reyn/plugin-data/<name>/`, a
     SIBLING of `~/.reyn/plugins/` (never a subdirectory of the copy
     itself, since step 5's atomic swap replaces that copy wholesale on
     every reinstall/update — anything a plugin needs to SURVIVE a
     reinstall cannot live inside it), created eagerly on every bake.
   - `pipelines/*.yaml`: unchanged — the pre-#4570 reyn-native `${REYN_*}`
     full-text bake (P1 `reyn.plugins.tokens.expand_reyn_tokens`, every
     token the copy-time context carries a value for). Pipelines are a
     reyn extension the standard doesn't define, so conversion D
     deliberately leaves this candidate untouched.
   - `skills/*/SKILL.md`: a NARROWER bake, only `${REYN_PLUGIN_ROOT}`
     (`plugin_install.py`'s `_bake_plugin_root_only`) — `${REYN_SKILL_DIR}` and
     `${REYN_PROJECT_DIR}` are deliberately left as literal tokens for the
     skill-load verb (`reyn.plugins.skill_load.load_skill_body`, P4/#3070) to
     resolve fresh at every invocation, since the plugin's global
     `~/.reyn/plugins/<name>/` copy can be enabled into more than one project
     (§3.3) — baking one install call's project into the shared copy would
     freeze every later enabling project to whichever one installed it first.

   **Stale-token warnings (#4610):** the two vocabularies above are
   per-FILE, not interchangeable — a plugin author guessing the wrong
   token for a given file (`${PLUGIN_ROOT}` inside `pipelines/*.yaml` or
   `SKILL.md`; `${REYN_PLUGIN_ROOT}`/`${REYN_PROJECT_DIR}`/`${REYN_SKILL_DIR}`
   inside `mcp.json`'s `args`/`env`/`cwd`) previously survived as a silent
   literal string, expanded by neither bake. Each of the three bake
   functions now also scans its OWN expanded output for the OTHER
   vocabulary's token shape (`_stale_token_warnings`) and, if found,
   appends a warning naming the file and the correct token for that
   location — never firing on `mcp.json`'s `command`/`url` (intentionally
   unexpanded, not a guess) or on a token the bake doesn't touch at all.
   Report-only: never blocks the install, surfaced via the result dict
   (below), same "present but empty when nothing to report" shape as
   `skipped` (#4580). Each finding also emits `plugin_install_token_
   vocabulary_mismatch` (payload: `name`, `warning` — the identical string
   the result dict carries, no re-derivation) — the durable audit-trail
   counterpart, so a plugin installed once and never re-inspected still
   has a record (#4610 PR-2).
7. Register (#3209: register-only, no dep materialise step; #4570
   conversion B: capability presence is derived from directory/file
   EXISTENCE, not a manifest-declared list — the manifest carries no
   `capabilities` field at all any more): `.mcp.json`'s successor,
   `mcp.json`, is always probed (no-ops gracefully when absent) via
   `.reyn/config/mcp.yaml` (probe-then-commit, mirrors `mcp_install_local`);
   a `pipelines/` directory, if present, has every `*.yaml` file registered
   via `pipeline_install.handle`; a `skills/` directory, if present, has
   every subdirectory registered via `skill_install.handle` (each sub-op
   carries `plugin_id=<name>`, §3.7). A server's `command` is registered
   AS-IS, no venv-interpreter rewrite. Emit `plugin_install_registered`.
   Before #4580, this step's own `registered` list was the ONLY signal
   — a plugin directory containing 3 capability kinds that only 2
   actually registered read identically to "present 3, registered 3": no
   count or event let an operator (or a test) tell the two apart (this
   was true back when the 3 came from a manifest-declared list; #4570
   conversion B later changed WHERE the count comes from, not the
   registered/skipped distinction itself). The return value's
   `registered`/`skipped` dicts (both keyed by capability kind —
   `mcp`/`pipelines`/`skills`) now record every present capability kind
   that did NOT make it in, by a different mechanism per kind: mcp's
   probe-then-commit skips BEFORE the write (a probe failure or a
   denied MCP-axis permission gate — `mcp_server_install_skipped`, #4580);
   a pipeline/skill sub-install always runs and is routed to `skipped`
   when its OWN return value's `status` isn't `"installed"` (a bad name,
   a threat-scan block, a missing DSL file, ...) — `pipeline_install_
   skipped`/`skill_install_skipped` (#4590). Either way, "present but
   not registered" is visible in the return value and its own audit-event,
   never a silent drop.
8. Delete the `_install_state.json` marker (absence = completed) and emit
   `plugin_install_completed`.

`plugin_uninstall` handler lifecycle (drop-registry-first, §3.11 — an
interrupted uninstall never leaves a live registry entry pointing at a
deleted copy):
1. Drop every `.reyn/config/{mcp,pipelines,skills}.yaml` entry tagged
   `plugin_id == name` (gated `require_file_write` per config file actually
   touched). Emit `plugin_uninstall_registry_dropped`.
2. Remove the `~/.reyn/plugins/<name>/` copy (gated `require_file_write`).
   Emit `plugin_uninstall_completed`.

**Plugin data survives uninstall** (#4570 conversion D): a plugin's
`~/.reyn/plugin-data/<name>/` directory (above, step 6) is NEVER deleted
by `plugin_uninstall` — data outliving code is the safe direction; the
reverse (code reinstalled later, its old data silently gone) is
unrecoverable. Not silent either: when a data directory exists for the
uninstalled name, the result dict carries `plugin_data_retained_at`
naming exactly where it is.

**Not WAL-derived** (§3.11): the `~/.reyn/plugins/` copies are FILES, not
WAL-event-derived state — the CLAUDE.md truncate-falsify recovery gate does
not apply to them. The reconcile above is a filesystem/registry consistency
check; the registry entries THEMSELVES still ride the existing
config-generation recovery path via `skill_install` / `pipeline_install`.

Result fields (`plugin_install`): `status` (`"installed"` / `"skipped"` /
`"error"`), `name`, `plugin_root`, `source_kind`, `capabilities`, `registered`
(per-capability sub-results), `stale_token_warnings` (list of strings, empty
when the bake found nothing stale — #4610, above).

Result fields (`plugin_uninstall`): `status` (`"uninstalled"` / `"error"`),
`name`, `removed` (per-registry list of dropped entry names), `copy_removed`,
`plugin_data_retained_at` (present only when a `~/.reyn/plugin-data/<name>/`
directory exists for the uninstalled name, #4570 conversion D).

Events emitted: `plugin_install_reconciled` (step 0, above — self-healing
rollback of a stale prior install, fires BEFORE `_started` and only when
one is found) / `plugin_install_started` / `_copied` / `_registered` /
`_completed` / `_token_vocabulary_mismatch` (#4610, one per stale-token
finding, above); `plugin_uninstall_started` / `_registry_dropped` /
`_completed`.

## `embed`

Raw embedding primitive (FP-0057 Phase 1): a batch of texts in, one vector per text out, in the same order. `embed` is the **user-facing** primitive — the user composes `embed` -> their own external MCP vector-DB's store/search tools via pipeline (reyn hosts no user RAG store). It is also the SHARED logic later internal RAG ops (`index_update` / `semantic_search`, FP-0057 Phase 2) call — same `EmbeddingProvider`, no duplicated embed logic, split only by audience surface.

```json
{
  "kind": "embed",
  "texts": ["first chunk of text", "second chunk of text"],
  "embedding_model": "standard"
}
```

Fields:

- `texts` (list[str], required) — texts to embed. Returned vectors preserve this order.
- `embedding_model` (str, default `"standard"`) — model class (light/standard/strong) or a literal provider model id, forwarded to `EmbeddingProvider.embed`.

Returns: `{"kind": "embed", "vectors": list[list[float]], "model": str, "total_tokens": int, "cost_usd": float | None, "priced": bool}`. On cancel: `{"kind": "embed", "status": "cancelled", "model": str}` (see **Bound + cancel** below).

`cost_usd` / `priced` (FP-0063 PC): the call's cost, priced via `estimate_embedding_cost` (extends the same `litellm.model_cost` lookup `pricing.py` already used for chat completions to embedding-mode entries — not a new rate table). `priced=False` + `cost_usd=None` when litellm cannot price `model` — an unpriced/unknown model degrades VISIBLY (never a silent `$0.00`, mirroring the pre-existing `estimate_cost` unknown-model sentinel, #1829). This spend is recorded into an INDEPENDENT embedding-cost aggregate (`EmbeddingCost` in `llm/pricing.py`) via `ctx.budget_gateway` (when wired) — the SINGLE recording entry point (`BudgetGateway.record_embedding`), which fans out to all scopes itself: session (the gateway's own aggregate) and agent/project (the process-shared `BudgetTracker` it holds). The fan-out lives in the gateway because it is the only object holding BOTH the tracker and the session's agent NAME, which is the key the per-agent counters use — an op handler has only `ctx.agent_id` (the FP-0016 host identity, a different value), so recording from there would file spend under a key no reader looks up. Deliberately **not** folded into the chat `CostBreakdown` (embedding is input-only / structurally uncacheable; doing so would dilute `cache_hit_rate` / `cache_savings`, which are chat-call-only figures). See `Registry.agent_embedding_cost` / `.project_embedding_cost` and `BudgetGateway.embedding_cost` for the per-scope readers.

Reuses the existing `EmbeddingProvider` (`LiteLLMEmbeddingProvider` via `get_provider` — the sole embedder; #3128 removed the in-process sentence-transformers backend and its `RoutingEmbeddingProvider` prefix-dispatch wrapper, so `get_provider` now returns the litellm-backed provider directly); this op is a thin typed envelope, not a re-implementation. Batching (`embedding.batch_size`, default 100) happens inside the provider — the op contract itself is list-in/list-out, batch-granular.

**Redaction-egress seam**: embedding via an API-backed provider sends text content to an external embedding API — a data-egress point. Every text in the batch is passed through a PRE-embed scan (`redact_secrets`, the existing FP-0050 secret-redaction primitive) *before* `provider.embed()` is called, unconditionally (no caller-supplied bypass). A redaction hit fires an `embed_secret_redacted` audit-event. This is a Phase 1 scaffold of the seam using the existing generic secret-redaction pass; the full firm ephemeral-attachment content policy is FP-0057 Phase 3.

**Bound + cancel** (#3043): like every other provider call in the OS, an embed is bounded and cancellable. The **bound** is `embedding.timeout` (default 60.0s, `<= 0` opts out), applied **per attempt** inside the provider — so it covers every caller of the provider, not just this op. Without it the only ceiling was litellm's own `request_timeout` default (6000s/attempt, ~5h across `max_retries`), which an operator cannot distinguish from a hang.

The bound is a **latency** invariant, not a **cost** one: it caps how long reyn waits, not how many requests the provider receives. Pre-fix the OpenAI SDK client's own retry (`max_retries=2`, litellm's implicit default) sat *under* the bound, so one attempt could deliver up to 3 requests and `max_retries: 3` up to 9 — measured delivered in 7.6s under the default 60.0s bound, which never engages. [#3054](https://github.com/tya5/reyn/issues/3054) closed that lever: `_aembedding_bounded` sets `litellm.DEFAULT_MAX_RETRIES = 0` (a falsy `max_retries=0` kwarg alone revives the `x or DEFAULT` trap), so the SDK-internal retry is disabled and reyn's own `_embed_batch_with_retry` loop is the ONLY retry layer — `max_retries: 3` now means 3 delivered requests, not 9. Lowering `timeout` still does not change that count; the two are separate levers. The *residual* under-count — the cost tracker records only the ONE returned response's tokens, so a call that retried N times before succeeding silently reports 1-of-N delivered requests — is made OBSERVABLE (not priced) by the `embed_attempts` audit-event (#3047 (c), observation-only: it never touches the cost aggregate, so it cannot double-count). The **cancel** seam is at this op: `provider.embed()` is raced against `ctx.cancel_event` via `race_cancellable` (the same primitive `mcp` and `sandboxed_exec` use), so a Ctrl-C interrupts the in-flight HTTP read immediately rather than waiting out the bound. This op is the right altitude for the cancel half because every embedding egress funnels through it (`semantic_search`, `index_update`, and the action-index all dispatch `embed` rather than calling `provider.embed()` provider-direct — the same property the redaction-egress seam relies on).

Events: `embed_secret_redacted` (`count`, `model`) when the PRE-embed scan redacts one or more texts. `embed_cancelled` (`model`) when `cancel_event` fires mid-embed — a cancelled outcome, distinct from a provider fault (mirrors `mcp_cancelled` / `sandboxed_exec_cancelled`). `embed_attempts` (`model`, `attempts`, `successful_batches`) on every successful embed (#3047 (c)): `attempts` is how many times reyn's own retry loop reached the provider call (summed across internal batches), `successful_batches` how many returned — so `attempts - successful_batches` is the retry overhead the cost tracker cannot see (it prices only the returned response). Always emitted on success, even with zero retries (`attempts == successful_batches`), so an absent event means "not instrumented", never "zero retries". Provider-supplied `attempts` is `NotRequired` on `EmbedBatchResult` — a loopless provider omits it and the op simply does not emit (no fabricated `attempts=1`); the op reads it defensively. This is reyn's retry-loop altitude, not a raw wire-request count — the two coincide only while #3054's `max_retries=0` holds the SDK-internal retry at 0.

Default-**ALLOW** (compute op — the cost is the embedding API/compute, not a workspace write); individually name-gateable, as a registered router-callable tool, by a per-session contextual narrowing at the RouterLoop gate (`effective.tool_contextually_denied`). At Phase 1 this op was additive and did not retire `embed_and_index` (`reyn.api.safe.embed_index`, the CodeAct-only ingestion entry); that clean-break landed in FP-0057 Phase 2b — `embed_and_index` is deleted, and both `index_update` (ingestion) and `semantic_search` (query) now dispatch their embed calls through THIS op (see [`index_update`](#index_update) below).

## `index_query`

Semantic similarity search over a single indexed source.

```json
{
  "kind": "index_query",
  "source": "project_docs",
  "query_vector": [0.1, 0.2, ...],
  "top_k": 5,
  "filters": {"path": "docs/concepts"}
}
```

Fields:

- `source` (str, required) — logical source name.
- `query_vector` (list[float], optional) — pre-computed embedding. If `null`, falls back to catalog enumeration (up to `fallback_size_cap` tokens).
- `top_k` (int, default `5`) — number of results to return.
- `filters` (dict[str, str], optional) — metadata key/value filters applied before ranking.
- `fallback_size_cap` (int, default `4096`) — token cap for enumerate fallback when `query_vector` is `null`.

Returns: `{"kind": "index_query", "source": str, "results": [{"text": str, "score": float, "metadata": dict}]}`.

## `semantic_search`

Macro op: embed a query → call `index_query` per source → merge and return top-K results globally. The preferred high-level op for RAG retrieval. **FP-0057 Phase 2a: renamed from `recall`** (clean break — fixes the observed `recall`/`search_actions`/`memory` naming collision; no compat alias).

```json
{
  "kind": "semantic_search",
  "query": "How does crash recovery work?",
  "sources": ["project_docs", "api_reference"],
  "top_k": 5,
  "embedding_model": "standard"
}
```

Fields:

- `query` (str, required) — natural-language query to embed and search.
- `sources` (list[str], required) — logical source names to search. Must not be empty.
- `top_k` (int, default `5`) — number of results returned after the merge/combine step (see below).
- `filters` (dict[str, str], optional) — forwarded to each `index_query` sub-op.
- `embedding_model` (str, default `"standard"`) — fallback model class used ONLY when a source has no recorded model yet (an empty/unindexed source). An already-indexed source's OWN recorded model always wins.

**Multi-model correctness (co-vet #1, load-bearing):** each source's embedding model is **auto-adopted** from its recorded index (`SourceManifest.embedding_model`, falling back to the SQLite backend's `stat().embedding_model`) — never caller-supplied per source. Sources are grouped by DISTINCT resolved model; the query is embedded **once per distinct model** (not once total, not once per source), and each source is queried with its matching model's vector. Cosine scores from different embedding spaces are not commensurable, so the merge is two-tiered: **within** a model group, chunks are merged and sorted by score (safe, same space — this is byte-identical to the pre-rename single-model `recall` behaviour when all sources share one model); **across** groups, each group's already-ranked top-K is combined via an order-preserving round-robin interleave (group order = first appearance in `sources`), capped at `top_k` — raw score magnitudes are never compared across groups.

Returns: `{"kind": "semantic_search", "chunks": [{"text": str, "score": float, "metadata": dict}], "mode": "semantic" | "fallback" | "mixed"}`.

Events: `semantic_search_embed_failed` if a model group's embed call fails (query, model, error).

## `index_drop`

Removes an indexed source entirely — deletes its SQLite backend and manifest entry. **Destructive and irreversible.** Requires `permissions.index_drop: ask` (or explicit `allow`) in skill frontmatter, and triggers a user-approval gate by default.

```json
{
  "kind": "index_drop",
  "source": "project_docs"
}
```

Fields:

- `source` (str, required) — logical source name to drop.

Returns: `{"kind": "index_drop", "source": str, "chunks_dropped": int}`.

Events: `index_dropped` (`source`, `chunks_dropped`).

## `index_update`

Incremental / delta-reconcile ingestion into a source's `IndexBackend` (FP-0057 Phase 2a). **NO full-rebuild mode** — a from-scratch rebuild is `index_drop` → `index_update` on the now-empty source. The caller (a chunker) supplies pre-chunked `chunks`; each chunk carries `content_hash` + `source_path` in its `metadata`. Reconciled against the source's current index, content-addressed by `content_hash` within each `source_path`:

- **add** — new `content_hash`, new `source_path` → embed (via the `embed` op — same primitive, no duplicated embed logic) + insert.
- **update** — new `content_hash`, `source_path` already indexed (content changed) → embed + insert; the path's stale hash(es) are removed in the same pass.
- **remove** — an indexed hash whose `source_path` IS among this call's chunks but whose hash is NOT → deleted. Scoped to the `source_path`s THIS call supplies chunks for — a path never mentioned is left untouched (a partial re-ingest of a few files never mass-deletes the rest of the source).
- **skip** — `content_hash` already indexed → no-op (no re-embed).

```json
{
  "kind": "index_update",
  "source": "project_docs",
  "chunks": [
    {"text": "...", "metadata": {"content_hash": "abc123", "source_path": "docs/a.md"}}
  ],
  "embedding_model": "standard"
}
```

Fields:

- `source` (str, required) — logical source name to ingest into.
- `chunks` (list[dict], default `[]`) — chunks to reconcile; each `{text, metadata}` with `metadata.content_hash` / `metadata.source_path` required.
- `embedding_model` (str, default `"standard"`) — used ONLY when this source has no recorded model yet (first `index_update` for a new source) — an already-indexed source's recorded model always wins (a source is one embedding space).
- `description` / `path` (str, optional) — `SourceManifest` fields, set on first index or override.

**Source-model-bound**: the source's embedding model is recorded on first ingestion and reused on every subsequent `index_update` call for that source.

**Cost surfacing**: `EmbeddingProvider.estimate_tokens` is consulted on the to-embed batch (post pre-embed dedup skip) and compared against `embedding.cost_warn_threshold` (`reyn.yaml`). Exceeding it does not block the op — it emits an `index_update_cost_warning` audit-event and the returned envelope carries a `cost_warning` field, so a large ingestion surfaces its cost instead of embedding silently.

Returns: `{"kind": "index_update", "source": str, "added": int, "updated": int, "removed": int, "skipped": int, "chunk_count": int, "embedding_model": str, "cost_warning": dict | null}`.

Events: `index_update_cost_warning` (`source`, `chunk_count`, `estimated_tokens`, `threshold`) when the to-embed batch exceeds the configured threshold; `index_updated` (`source`, `added`, `updated`, `removed`, `skipped`) on completion.

Default-**ALLOW** (own-write op — writes only to the source's OWN index + manifest, not a destructive cross-cutting op like `index_drop`).

## `compact`

Voluntarily compact the conversation history *now*, freeing context window.
The OS injects a **context-size signal** (a `## Context window` header with
the exact-token free window) when the window is filling; the model may
respond by emitting `compact` instead of waiting for the mandatory `retry_loop`
backstop. The op routes to the caller-wired compaction (`force_compact_now`)
and reports the freed tokens + the free window afterwards, in exact tokens
(unit-aligned with the media load-contract error so "should I compact" and
"what fits now" use the same scale).

```json
{
  "kind": "compact"
}
```

Fields:
- `reason` (str, optional): Short model-supplied rationale for the audit trail. The OS never interprets it.

Returns:
- `status: "ok" | "error"`
- `freed_tokens: int` — exact-token reduction. **~0 by construction**: the router prompt is head+tail *turn*-count bounded (`_build_history_for_router`), so compaction does not shrink the bounded view; it compresses the already-elided middle into a summary bridge. Don't front `freed_tokens` here — see the compression metric below.
- `free_window_after` / `free_window_before: int` — exact-token headroom after / before.
- **Compression metric** (the meaningful signal): `summarized_turns: int` (older turns folded into the bridge), `compressed_tokens: int` (their raw token cost), `bridge_tokens: int` (the summary's token cost). The value that matters is the `compressed_tokens → bridge_tokens` compression, not `freed_tokens`.
- On error: `error_kind` (`compaction_unavailable` when no compaction context is wired here; `compaction_failed`) + `error`.

**Events**: `compact_op_requested` / `compact_op_completed` (`freed_tokens`, `free_window_after`, `summarized_turns` / `compressed_tokens` / `bridge_tokens`) / `compact_op_failed` / `compact_op_unavailable` (P6). The inner compaction engine emits its own compaction audit-events.

**Permission**: none required (LLM cost only). Voluntary and independent of the involuntary `retry_loop` backstop, which always runs regardless.

**Visibility**: advertised to the LLM (tool / `available_control_ops`) only when the window is filling — paired with the context-size signal — so it is not offered when there is nothing to compact (mirrors the `search_actions` visibility gate). The permission gate stays "allow"; only *when surfaced* is gated.

## `emit_hook_event`

LLM-authored hook-event emission (Hook-Event Redesign Phase 5 part 2, proposal
[0059-hook-event-redesign.md](../../deep-dives/proposals/0059-hook-event-redesign.md)
§8/§8.4) — the FIRST place an LLM can put a `HookEvent` onto a live per-session
`HookBus` (Phase 4a); every prior producer (`HookDispatcher.dispatch` at the 10
builtin points, a `Composer`'s correlated output, the Ingress Adapters) is
OS-internal code, never an LLM tool call. Router-only (`gates.router="allow"`) —
the handler needs a live, session-bound `HookBus` + `session_id`, which only a
chat-router `OpContext` wires.

```json
{
  "kind": "emit_hook_event",
  "event_name": "deploy_ready",
  "payload": {"artifact": "build-42"}
}
```

Fields:

- `event_name` (str, default `""`) — the event's name; the router tool
  schema exposes ONLY this + `payload`. The emitted kind is ALWAYS
  `llm:<session_id>:<event_name>` — the session component comes SOLELY from
  `OpContext.session_id` at handler-execution time, never from an LLM-supplied
  value (there is no session field on this schema for the well-behaved
  tool-call path to set). Schema-constrained (#2890 F6):
  `pattern=^[A-Za-z0-9_.-]*$` + `max_length=200` — control characters,
  newlines, and unbounded length are rejected at Pydantic validation time
  (before the handler's own non-empty check ever runs), so they can never
  flow into the constructed `kind` or the `hook_event_emitted` audit-event.
  Defense-in-depth: the kind is already structurally confined to this
  session's own `llm:{session_id}:` prefix regardless.
- `target_kind` (str | None, default `None`) — a defense-in-depth escape
  hatch on the Pydantic model, **deliberately NOT exposed in the router
  tool's JSON schema** (unreachable from a normal LLM tool call). Exists so
  the kind whitelist below has a real, exercisable subject for any OTHER
  caller of this Op (e.g. a future Control-IR JSON surface), and so the
  security co-vet suite can test the reject path directly.
- `payload` (dict, default `{}`) — carried on the emitted `HookEvent` for a
  matcher / Composer to inspect; never rendered into a hook message template
  by this op itself (§8.4 item 1's `context_safe` template-interpolation
  discipline is Composer/render's concern, not emit's).

Returns: `{"kind": "emit_hook_event", "status": "ok", "emitted_kind": str}` on
success; `{"status": "denied", "error": str}` when the autonomy boundary
rejects the emit; `{"status": "error", "error": str}` for a malformed
`event_name`/`target_kind`.

Events: `hook_event_emitted` (`kind`, `session_id`, `event_id`) — metadata
only, mirrors `hook_push_fired`'s never-the-message-body discipline (the
payload may carry LLM-authored free text).

**The autonomy boundary (§8.4 item 3, the security crux) is enforced in TWO
SEPARATE dimensions, both BEFORE `HookBus.publish`** (`HookBus.publish` is
synchronous, never raises, and broadcasts to every live subscriber — there is
no downstream gate once an event reaches the bus; the handler
(`reyn.core.op_runtime.emit_hook_event`) is the ONLY defense line):

1. **KIND dimension** — a static OUT-set whitelist
   (`reyn.hooks.schema_registry.is_emittable_llm_kind`, an ALLOW-list, not a
   DENY-list): only this session's own `llm:<session_id>:*` namespace may
   ever be emitted. `builtin:*` (spoofs Reyn's own lifecycle/ingress events),
   `composed:*` (spoofs a Composer's CORRELATED output — letting an LLM fire
   a `composed:*`-only hook, e.g. an approval-gated deploy, WITHOUT the
   Composer's actual correlation logic ever running), `webhook:*`/`mcp:*`
   (spoof external ingress), and another session's `llm:*` are all rejected.
2. **SESSION dimension** — structural for the normal (`event_name`) path: the
   session component of the kind is built ONLY from `ctx.session_id`: nothing
   on the schema for a well-behaved tool call to override. The `target_kind`
   escape hatch is instead validated by the SAME whitelist — either way, the
   handler never looks up a bus by session id (`ctx.hook_bus` is a single
   fixed reference to THIS session's own bus), so there is no code path that
   could route a mismatched kind's event to a different session's bus even
   absent the whitelist check.

**`OpContext.hook_bus`** (this session's `HookBus`, Phase 4a) is the new seam
this op reaches through — threaded down the same Session → router / kernel
chain as `OpContext.hook_dispatcher` (mirrors that field's threading exactly:
`Session.__init__` constructs one `HookBus` per session and passes it into
`RouterHostAdapter` / `build_router_op_context`, exactly like
`hook_dispatcher`). Downstream, an emitted event a Composer correlates into
`composed:*` traverses the EXISTING `composed:*` → `ComposedEventConsumer` →
`HookDispatcher.dispatch_bus_event` → inbox `kind="hook"` E-path (Phase 5 part
1, #2881) unchanged — the `max_hook_driven_turns` loop-valve counts an
emit-origin wake turn with ZERO new bounding logic, the same "every wake path
traverses `kind="hook`" invariant Phase 5 part 1 already pins.

**Out of scope for this phase** (tracked in #2884, a separate recovery-gated
arc): making `_hook_driven_turns` (the loop-valve counter) WAL/snapshot-backed
across a crash. It remains in-memory-only (proposal §11 future list item 2);
`emit_hook_event` increases the NUMBER of hook-driven-turn-generating paths
but does not change this counter's crash-durability posture. #2884 additionally
tracks a NEW risk dimension this phase's producer surfaces: a WAL-replay-driven
re-emit (an `emit_hook_event` op re-executed during crash-recovery WAL replay)
is a distinct hazard from the counter's own in-memory reset, and is
out-of-scope here too.

---

## `describe_session`

Read-only session introspection (#5012-A). No arguments, no side effect, no
permission gate (`gates.router="allow"`, mirrors `list_actions`/
`search_actions` — always advertised, unconditional).

Field population is closed to 3 fields (architect ruling, `gh issue view
5012`, 2026-08-21 — never grown ad hoc):

1. **`write_scope`** — what `sandbox.policy` DECLARES, never the resolved/
   effective scope (a context-free tool cannot know the caller-supplied
   `write_paths` floor `resolve_sandbox_policy()` needs — same reasoning
   `doctor.py`'s C-5 check documents, #4364). Three-way discriminator:
   `{"declared": false}` (no `sandbox.policy` block at all — this repo's own
   live default) / `{"declared": true, "allow_write_paths": null,
   "deny_write_paths": null}` (block present, no write-scope keys) /
   `{"declared": true, "allow_write_paths": [...], "deny_write_paths":
   [...]}` (the block's own declared values, verbatim).
2. **`position`** — `{repo_root, branch, head, python_executable, venv_path,
   capability: {ruff, pytest, mkdocs}}`. `branch`/`head` are `null` for a
   non-git directory or before the first commit; `branch` is `null` (not the
   literal `"HEAD"`) in a detached-HEAD state.
3. **`auth_status`** — one entry per provider declared in `auth.providers`
   (reyn.yaml), each `{authenticated: bool, reason: str}` — NEVER a token,
   refresh token, client secret, or scope. Scoped PERMANENTLY to reyn's own
   `reyn auth` OAuth device-grant flow — a third-party CLI's own auth (`gh
   auth status`, `aws`, `gcloud`) is out of scope by the owner's "does
   reyn's code grow when the tool's own case count grows" rule; an agent
   that needs it can run that CLI directly via `sandboxed_exec`.

```json
{"kind": "describe_session"}
```

Result:

```json
{
  "kind": "describe_session",
  "status": "ok",
  "write_scope": {"declared": false},
  "position": {
    "repo_root": "/path/to/repo", "branch": "main", "head": "abc1234...",
    "python_executable": "/path/to/.venv/bin/python",
    "venv_path": "/path/to/.venv",
    "capability": {"ruff": true, "pytest": true, "mkdocs": true}
  },
  "auth_status": {}
}
```

Implementation: `reyn.runtime.session_write_scope.describe_write_scope`,
`reyn.runtime.session_position.describe_session_position`,
`reyn.runtime.session_auth_status.describe_auth_status`, assembled by
`reyn.core.op_runtime.describe_session`. `OpContext.sandbox_config` /
`OpContext.auth_config` are narrow config-derived projections (same shape
as `OpContext.threat_scan`), threaded through
`build_router_op_context`/`RouterOpContextSource` from `Session`'s own
`_sandbox_config`/`_auth_config`.

---

**Note for contributors:** When adding a new Control IR op kind to `src/reyn/schemas/models.py` and `src/reyn/core/op_runtime/registry.py`, **also add a section here** in the same PR. The reference and the registry must stay in sync — see [CLAUDE.md](https://github.com/tya5/reyn/blob/main/CLAUDE.md) for the rule.

## Where ops are exposed to the LLM

Each op kind's schema and description are held on a `ToolDefinition` (per capability) in the unified `ToolRegistry`. `render_for_router()` renders these into the OpenAI-style `tools[]` array `build_tools()` assembles for the chat router — the LLM picks ops by matching its intent to those descriptions. See [LLM invocation surfaces](../../concepts/architecture/llm-invocation-surfaces.md) for the full mechanism.

## See also

- [events.md](events.md) — events emitted per op kind
