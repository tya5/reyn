---
type: reference
topic: runtime
audience: [human, agent]
---

# Control IR

Control IR is the list of side-effect operations the LLM may emit alongside its artifact. The OS dispatches each op and returns the result for the LLM (or the next phase) to consume.

## Op kinds

| Kind | Purpose | Permission required |
|------|---------|---------------------|
| `read_file` | Read a file (optionally a line range) | `file.read` |
| `write_file` | Write (create / overwrite) a file | `file.write` |
| `edit_file` | Replace a string in a file | `file.write` |
| `delete_file` | Delete a file | `file.write` |
| `glob_files` | List files matching a glob pattern | `file.read` |
| `grep_files` | Search file contents by regex | `file.read` |
| `ask_user` | Pause the phase and ask the user a question | none (always allowed) |
| `run_skill` | Run another skill as a sub-workflow | none (skill-level decision) |
| `lint` | Run the DSL linter on a skill directory | none |
| `shell` | Run a shell command (**deprecated** — use `sandboxed_exec`) | `shell` (off by default; needs `--allow-shell`) |
| `sandboxed_exec` | Run argv under a `SandboxPolicy` via a `SandboxBackend` | enforced by backend (`SandboxPolicy`) |
| `web_search` | Search the public web via DuckDuckGo | Tier 1 — default allow; `web.search: deny` in `reyn.yaml` blocks |
| `web_fetch` | Fetch a single URL and return extracted text | Tier 1 — default allow; `web.fetch: deny` in `reyn.yaml` blocks |
| `mcp` | Call a tool on a configured MCP server | `permissions.mcp: [server_name]` in skill frontmatter |
| `mcp_install` | Install an MCP server from the registry into the project config | `permissions.mcp_install: true` in skill frontmatter |
| `mcp_drop_server` | Remove an MCP server from project/local/user config (inverse of `mcp_install`) | `permissions.mcp_drop_server: true` in skill frontmatter |
| `embed` | Embed texts or artifact chunks via a LiteLLM embedding model | none (embedding API cost) |
| `index_write` | Write embedded chunks to an index backend (SQLite) | none |
| `index_query` | Semantic vector search over one indexed source | none |
| `recall` | Macro: embed → index_query per source → merge top-K | none |
| `index_drop` | Remove an indexed source entirely (destructive) | `permissions.index_drop: ask` in skill frontmatter |
| `judge_output` | LLM scorer: rubric + threshold + `on_fail` policy | none (LLM cost) |
| `skill_resolve` | Resolve a skill name to its on-disk path (read-only) | none |
| `compact` | Voluntarily compact the conversation/phase history (advisory) | none (LLM cost; the mandatory `retry_loop` backstop is independent) |

## Common envelope

Every op is a JSON object with a `kind` discriminator:

```json
{
  "kind": "read_file",
  "path": "src/foo.py"
}
```

The OS validates the op against its kind's schema, executes it, and returns a result to the calling phase.

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
| `read_file` | `file.read` | `offset` / `limit` (line range) optional. |
| `write_file` | `file.write` | Creates or overwrites; parent dirs created as needed. |
| `edit_file` | `file.write` | `old_string` must be unique unless `replace_all: true`. |
| `delete_file` | `file.write` | |
| `glob_files` | `file.read` | `path` defaults to `.`. |
| `grep_files` | `file.read` | `glob` filters which files are searched. |

Permission scopes are configured per-op kind. See `reference/config/permissions.md`.

### The coarse `file` execution backend (not phase-emittable)

The fine kinds above are the only file ops a phase advertises to (and accepts
from) the LLM. They are dispatched through the unified ToolRegistry, then build
a coarse `FileIROp` (`{kind: "file", op: ...}`) internally and route to the
shared `op_runtime/file.py` backend. That coarse `file` kind — dropped from
`OP_KIND_MODEL_MAP` in #1240 Wave 2b — is **not** an LLM-emittable Control IR
kind. It survives only as:

- the shared execution backend the fine handlers delegate to, and
- the target of OS-deterministic preprocessor `run_op` steps
  (`{kind: file, op: ...}`), the chat host file methods, and the `reyn memory`
  CLI.

Those non-phase callers also reach extended sub-ops the fine kinds do not
expose — `mkdir`, `move`, `stat`, and `regenerate_index` (used by `reyn memory`
and memory-managing skills via the preprocessor / CLI, never as phase Control
IR).

## `ask_user`

Pauses the phase and asks the user. The OS prints the question, reads stdin, and re-runs the *same phase* with the answer merged into the input as a `user_message` artifact. Visit count does not increment.

```json
{
  "kind": "ask_user",
  "question": "Which model do you want to target?",
  "suggestions": ["light", "standard", "strong"]
}
```

## `run_skill`

Runs another skill as a sub-workflow. The result is returned as a structured artifact for the calling phase to use.

```json
{
  "kind": "run_skill",
  "skill": "recall_memory",
  "input": {"type": "user_message", "data": {"text": "what did I tell you about my preferences?"}}
}
```

> **Advertised name.** Phases advertise this op to the LLM under the chat-tool
> name `invoke_skill` (so the catalog is uniform with the chat router). The OS
> aliases the emitted `invoke_skill` name back to the `run_skill` kind at the
> parse boundary (#1240). `run_skill` remains the canonical kind in
> `OP_KIND_MODEL_MAP` and on the dispatched op.

For deterministic invocation from a phase's preprocessor (rather than LLM-driven), use the `run_skill` preprocessor step instead — see `reference/dsl/preprocessor.md`.

## `lint`

Runs the DSL linter on a skill directory. Used by skill-building skills (`skill_builder`, `skill_improver`) to verify their output.

```json
{
  "kind": "lint",
  "skill_path": "reyn/local/my_skill"
}
```

## `shell`

Executes a shell command. **Off by default.** The runtime must be started with `--allow-shell` AND the project must permit `shell` in `reyn.yaml` (or grant per-run via prompt).

```json
{
  "kind": "shell",
  "cmd": "reyn run my_skill 'hello'",
  "timeout": 120
}
```

If shell is denied, the OS emits `shell_not_allowed` and returns a denial result rather than failing the phase.

**Deprecated.** Will be removed in 1.0. Use `sandboxed_exec` (below) — it routes through a `SandboxBackend` that enforces the declared `SandboxPolicy`. A `DeprecationWarning` is emitted on first `shell` invocation per skill.

## `sandboxed_exec`

Executes `argv` under a declared `SandboxPolicy` via the OS's selected `SandboxBackend`. Replaces `shell` for cases that need (or will need, once `SeatbeltBackend` / `LandlockBackend` land) real isolation enforcement.

```json
{
  "kind": "sandboxed_exec",
  "argv": ["echo", "hello"],
  "network": false,
  "read_paths": ["{{workspace}}"],
  "write_paths": ["{{workspace}}/output"],
  "allow_subprocess": false,
  "env_passthrough": ["PATH"],
  "timeout_seconds": 60
}
```

Fields:
- `argv` (required) — command + arguments. `argv[0]` is the executable.
- `network` (optional, default `false`) — allow outbound network.
- `read_paths` (optional) — filesystem paths the process may read (glob patterns OK).
- `write_paths` (optional) — filesystem paths the process may write.
- `allow_subprocess` (optional, default `false`) — may spawn children.
- `env_passthrough` (optional) — env-var names that pass through (others are stripped).
- `timeout_seconds` (optional, default `60`) — wall-clock cap.

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
  "prompt": "extract the key findings",
  "max_length": 50000
}
```

Fields: `url` (required), `prompt` (optional hint describing what to extract — informational for the LLM, not executed by the OS), `timeout` (optional, default `30` seconds), `max_length` (optional, default `50000` characters).

HTML responses are text-extracted (scripts, styles, and non-content tags stripped). If the content exceeds `max_length`, it is truncated and `truncated: true` appears in the result. Non-HTML responses are returned as-is.

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

> **Advertised name.** As with `run_skill`/`invoke_skill`, phases advertise
> this op to the LLM under the chat-tool name `call_mcp_tool`; the OS aliases it
> back to the `mcp` kind at the parse boundary (#1240). `mcp` remains the
> canonical kind in `OP_KIND_MODEL_MAP` and on the dispatched op.

The OS resolves the server's transport (`stdio`, `http`, or `sse`), dispatches via `MCPClient`, and returns the tool result. Every call emits `mcp_called`, `mcp_completed`, and (on failure) `mcp_failed` events.

See [concepts/tools-integrations/mcp.md](../../concepts/tools-integrations/mcp.md) for server configuration, transport options, and the security model.

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
- `server_id` (required) — registry identifier (e.g. `"io.github.foo/bar-mcp"`).
- `scope` (optional, default `"local"`) — config tier to write to:
  - `"local"` → `<project>/.reyn/config.yaml`
  - `"project"` → `<project>/reyn.yaml`
  - `"user"` → `~/.reyn/config.yaml`
- `env_overrides` (optional) — pre-supplied secret env values; skip interactive prompt
  for keys present here.

Handler lifecycle:
1. Fetches `server.json` via `RegistryClient`
2. Checks runtime command availability (`npx` / `uvx` / `docker` / `dnx`)
3. Gates via `PermissionResolver.require_file_write` (= `.reyn/mcp.yaml`) + `require_http_get` (= registry host); the legacy `require_mcp_install` bool-axis gate has been removed
4. Prompts for `isSecret=true` env vars via `intervention_bus`; each `save_secret` routes through `PermissionResolver.require_secret_write` (= Phase 6 wildcard `"*"` covers the runtime-determined key set)
5. Writes `mcp.servers.<name>` to the target scope config file
6. Emits `mcp_server_installed` event (P6) — key names only, no values

## `embed`

Embeds texts (or a JSONL artifact) into vectors using a LiteLLM-backed embedding model. Two input forms:

**Form A — inline** (small payload, e.g. recall query):
```json
{
  "kind": "embed",
  "texts": ["What is the capital of France?"],
  "model": "standard"
}
```

**Form B — artifact reference** (large payload, e.g. indexing many chunks):
```json
{
  "kind": "embed",
  "input_artifact": "chunks.jsonl",
  "text_field": "text",
  "output_artifact": "embedded_chunks.jsonl",
  "model": "standard"
}
```

Exactly one of `texts` / `input_artifact` must be provided. Fields:

- `texts` (list[str], Form A) — inline texts to embed.
- `input_artifact` (str, Form B) — workspace-relative JSONL path; each line must have a `text_field` key.
- `text_field` (str, default `"text"`) — field name to embed in Form B.
- `output_artifact` (str, Form B) — workspace-relative JSONL path for output vectors. Idempotent: lines whose `content_hash` already exists are skipped.
- `model` (str, default `"standard"`) — model class or LiteLLM string, resolved via `reyn.yaml embedding.classes`.

Returns: Form A → `{"kind": "embed", "vectors": [[float, ...]]}`. Form B → `{"kind": "embed", "status": "ok", "embedded": int, "skipped": int}`.

Events: `embed_progress` (Form B only, per batch — `embedded`, `skipped` cumulative counts).

## `index_write`

Writes embedded chunks to a named SQLite index backend. Two input forms:

**Form A — inline**:
```json
{
  "kind": "index_write",
  "source": "project_docs",
  "chunks": [
    {"text": "...", "vector": [0.1, 0.2, ...], "metadata": {"path": "README.md"}}
  ],
  "mode": "append"
}
```

**Form B — artifact reference**:
```json
{
  "kind": "index_write",
  "source": "project_docs",
  "input_artifact": "embedded_chunks.jsonl",
  "mode": "replace",
  "description": "Project documentation index",
  "path": "docs/**/*.md"
}
```

Fields:

- `source` (str, required) — logical source name. Maps to `.reyn/index/<source>/index.db`.
- `chunks` (list[dict], Form A) — inline list of `{text, vector, metadata}` objects.
- `input_artifact` (str, Form B) — workspace-relative JSONL path.
- `mode` (`"append" | "replace"`, default `"append"`) — `"replace"` drops the source first.
- `embedding_model` (str, optional) — override the embedding model recorded in chunk metadata.
- `description` (str, optional) — human-readable description stored in source manifest (shown in router system prompt).
- `path` (str, optional) — original glob or path stored in source manifest.

Returns: `{"kind": "index_write", "source": str, "chunks_written": int, "chunks_skipped": int}`.

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

## `recall`

Macro op: embed a query → call `index_query` per source → merge and return top-K results globally. The preferred high-level op for RAG retrieval.

```json
{
  "kind": "recall",
  "query": "How does crash recovery work?",
  "sources": ["project_docs", "api_reference"],
  "top_k": 5,
  "embedding_model": "standard"
}
```

Fields:

- `query` (str, required) — natural-language query to embed and search.
- `sources` (list[str], required) — logical source names to search. Must not be empty.
- `top_k` (int, default `5`) — number of results returned after global merge.
- `filters` (dict[str, str], optional) — forwarded to each `index_query` sub-op.
- `embedding_model` (str, default `"standard"`) — model class forwarded to the `embed` sub-op.

Returns: `{"kind": "recall", "results": [{"text": str, "score": float, "source": str, "metadata": dict}]}`.

Events: `recall_embed_failed` if the embed sub-op fails (query, error).

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

## `judge_output`

LLM-based output scorer for in-phase evaluation loops. Resolves a `target` dot-path to a value, calls an LLM with the caller-supplied `rubric`, and returns a score (0.0–1.0) plus a pass/fail flag.

```json
{
  "kind": "judge_output",
  "target": "artifact.data.summary",
  "rubric": "Score 0.0-1.0: is the summary concise, accurate, and complete?",
  "threshold": 0.8,
  "on_fail": "transition"
}
```

Fields:
- `target` (str, required): Dot-path to the value being scored (e.g. `"artifact.data.summary"`). Resolved against the current workspace artifact.
- `rubric` (str, required): LLM prompt body. Skill author writes the evaluation criteria. The OS never interprets this content (P7).
- `threshold` (float, optional, default `0.8`): Passing score in `[0.0, 1.0]`.
- `on_fail` (`"transition" | "abort" | "continue"`, optional, default `"transition"`):
  - `"transition"`: LLM picks next phase (existing decision flow).
  - `"abort"`: Abort skill execution.
  - `"continue"`: Score recorded only; no flow change.
- `model` (str | null, optional): Model class override (e.g. `"strong"`). Defaults to the skill's current model.

Returns: `{"kind": "judge_output", "score": float, "passed": bool, "reason": str, "threshold": float, "on_fail": str}`

Audit event: `tool_executed` with `op=judge_output, target, score, passed, threshold, reason` (P6).

**P7 note**: Reyn is rubric-agnostic. The rubric content is part of the skill's authored prompt; the OS only routes it to the LLM without inspection.

## `skill_resolve`

Resolve a skill name to its on-disk `skill.md` path via the canonical
resolution chain (`reyn/local/` → `reyn/project/` → `stdlib/`). Returns
path metadata; performs no content read.

```json
{
  "kind": "skill_resolve",
  "name": "skill_improver"
}
```

Fields:
- `name` (str, required): Short skill name (no slashes or `.md` extensions).

Returns:
- `name: str` — echo of input
- `resolved: bool` — `true` if `skill.md` exists in any resolution layer
- `skill_md_path: str | null` — absolute path to `skill.md`; `null` when unresolved
- `source: "local" | "project" | "stdlib" | null` — which resolution layer matched
- `skill_dir: str | null` — parent directory of `skill.md`; `null` when unresolved

**Events**: `skill_resolve_completed` (`name`, `resolved`, `source`) — emitted after every call (P6).

**Permission**: none required. The op is read-only (path existence walk within the trusted resolution chain); it never reads file content.

**OpPurity**: `world` (filesystem metadata read; result may vary if skills are added/removed between calls).

**Use case**: stdlib python steps that need a skill's absolute path can offload the filesystem walk to this op and stay in `mode: safe`. See R-PURE-MODE Class D refactor — `skill_improver/copy_to_work_resolver` and `eval_builder/analyze_skill_resolver` are the primary consumers.

---

## `compact`

Voluntarily compact the conversation/phase history *now*, freeing context
window. The OS injects a **context-size signal** (a `## Context window` header
with the exact-token free window) when the window is filling; the model may
respond by emitting `compact` instead of waiting for the mandatory `retry_loop`
backstop. The op routes to the caller-wired compaction (chat:
`force_compact_now`; phase: `compact_control_ir_results` on-demand seam) and
reports the freed tokens + the free window afterwards, in exact tokens
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
- `freed_tokens: int` — exact-token reduction. **Per-axis meaning (#191)**: on the **phase** axis this is the real `control_ir_results` shrink. On the **chat** axis it is **~0 by construction** — the router prompt is head+tail *turn*-count bounded (`_build_history_for_router`), so compaction does not shrink the bounded view; it compresses the already-elided middle into a summary bridge. Don't front `freed_tokens` for chat.
- `free_window_after` / `free_window_before: int` — exact-token headroom after / before.
- **Chat-axis compression metric** (the meaningful chat signal; `null` on the phase axis): `summarized_turns: int` (older turns folded into the bridge), `compressed_tokens: int` (their raw token cost), `bridge_tokens: int` (the summary's token cost). The chat value is the `compressed_tokens → bridge_tokens` compression, not `freed_tokens`.
- On error: `error_kind` (`compaction_unavailable` when no compaction context is wired here; `compaction_failed`) + `error`.

**Events**: `compact_op_requested` / `compact_op_completed` (`freed_tokens`, `free_window_after`, + chat-axis `summarized_turns` / `compressed_tokens` / `bridge_tokens`) / `compact_op_failed` / `compact_op_unavailable` (P6). The inner compaction engine emits its own compaction events.

**Permission**: none required (LLM cost only). Voluntary and independent of the involuntary `retry_loop` backstop, which always runs regardless.

**OpPurity**: `external` (LLM cost + history/state mutation; like `recall`, a macro whose inner engine emits its own events).

**Visibility**: advertised to the LLM (tool / `available_control_ops`) only when the window is filling — paired with the context-size signal — so it is not offered when there is nothing to compact (mirrors the `search_actions` visibility gate). The permission gate stays "allow"; only *when surfaced* is gated.

**Axis scope (chat vs phase)**: the `compact` op is available on **both** axes. On the **chat** axis, it routes to `force_compact_now`; on the **phase** axis, it routes to the `compact_control_ir_results` on-demand seam wired by the phase runtime (in addition to the automatic per-frame compaction that fires regardless). In both cases the OS wires `ctx.compact_now`; the op handler itself is axis-agnostic. Both axes also inject the paired context-size signal so the model knows when to emit `compact`.

---

**Note for contributors:** When adding a new Control IR op kind to `src/reyn/schemas/models.py` and `src/reyn/op_runtime/registry.py`, **also add a section here** in the same PR. The reference and the registry must stay in sync — see [CLAUDE.md](https://github.com/tya5/reyn/blob/main/CLAUDE.md) for the rule.

## Where ops are exposed to the LLM

The OS injects available ops into every context frame as `available_control_ops`. Each entry includes a `kind`, a one-line description, and a worked example. The LLM picks ops by matching its intent to descriptions — phase markdown MUST NOT describe op syntax (P8).

## See also

- [run.md](../cli/run.md) — `--allow-shell`, `--allow-unsafe-python`
- [events.md](events.md) — events emitted per op kind
- [Concepts: principles P8](../../concepts/architecture/principles.md#p8-phase-instructions-contain-only-domain-logic)
