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
| `file` | Read, write, glob, grep, edit, or delete files | `file.<op>` |
| `ask_user` | Pause the phase and ask the user a question | none (always allowed) |
| `run_skill` | Run another skill as a sub-workflow | none (skill-level decision) |
| `lint` | Run the DSL linter on a skill directory | none |
| `shell` | Run a shell command (**deprecated** — use `sandboxed_exec`, FP-0017) | `shell` (off by default; needs `--allow-shell`) |
| `sandboxed_exec` | Run argv under a `SandboxPolicy` via a `SandboxBackend` (FP-0017) | enforced by backend (`SandboxPolicy`) |
| `web_search` | Search the public web via DuckDuckGo | Tier 1 — default allow; `web.search: deny` in `reyn.yaml` blocks |
| `web_fetch` | Fetch a single URL and return extracted text | Tier 1 — default allow; `web.fetch: deny` in `reyn.yaml` blocks |
| `mcp` | Call a tool on a configured MCP server | `permissions.mcp: [server_name]` in skill frontmatter |
| `mcp_install` | Install an MCP server from the registry into the project config | `permissions.mcp_install: true` in skill frontmatter |

## Common envelope

Every op is a JSON object with a `kind` discriminator:

```json
{
  "kind": "file",
  "op": "read",
  "path": "src/foo.py"
}
```

The OS validates the op against its kind's schema, executes it, and returns a result to the calling phase.

## `file`

Sub-operations: `read`, `write`, `edit`, `delete`, `glob`, `grep`.

```json
{"kind": "file", "op": "read", "path": "src/foo.py"}

{"kind": "file", "op": "write", "path": "out.txt", "content": "..."}

{"kind": "file", "op": "edit", "path": "src/foo.py",
 "old_string": "...", "new_string": "..."}

{"kind": "file", "op": "delete", "path": "tmp.txt"}

{"kind": "file", "op": "glob", "pattern": "**/*.py"}

{"kind": "file", "op": "grep", "path": "src", "pattern": "def \\w+",
 "glob": "**/*.py", "output_mode": "content"}
```

Permission scopes are configured per-op kind. See `reference/config/permissions.md`.

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

**Deprecated by FP-0017.** Will be removed in 1.0. Use `sandboxed_exec` (below) — it routes through a `SandboxBackend` that enforces the declared `SandboxPolicy`. A `DeprecationWarning` is emitted on first `shell` invocation per skill.

## `sandboxed_exec`

Executes `argv` under a declared `SandboxPolicy` via the OS's selected `SandboxBackend` (FP-0017). Replaces `shell` for cases that need (or will need, once `SeatbeltBackend` / `LandlockBackend` land) real isolation enforcement.

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

Fetches a single URL and returns its text-extracted content. **Tier 1** — default allow; no permission declaration required (FP-0022). Typically used after `web_search` to read a result page in detail. Block with `web.fetch: deny` in `reyn.yaml`; pre-approve silently with `web.fetch: allow`.

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

The OS resolves the server's transport (`stdio`, `http`, or `sse`), dispatches via `MCPClient`, and returns the tool result. Every call emits `mcp_called`, `mcp_completed`, and (on failure) `mcp_failed` events.

See [concepts/mcp.md](../../concepts/mcp.md) for server configuration, transport options, and the security model.

## `mcp_install`

Installs an MCP server from `registry.modelcontextprotocol.io` into the project's config.
**Phase-only** (not available from the router). Requires `permissions.mcp_install: true`
in the skill's frontmatter **and** user approval (ADR-0029).

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
3. Gates via `PermissionResolver.require_mcp_install` (ADR-0029)
4. Prompts for `isSecret=true` env vars via `intervention_bus`; persists with `secrets.store`
5. Writes `mcp.servers.<name>` to the target scope config file
6. Emits `mcp_server_installed` event (P6) — key names only, no values

## `judge_output`

LLM-based output scorer for in-phase evaluation loops (FP-0007 Component D). Resolves a `target` dot-path to a value, calls an LLM with the caller-supplied `rubric`, and returns a score (0.0–1.0) plus a pass/fail flag.

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

**Note for contributors:** When adding a new Control IR op kind to `src/reyn/schemas/models.py` and `src/reyn/op_runtime/registry.py`, **also add a section here** in the same PR. The reference and the registry must stay in sync — see [CLAUDE.md](https://github.com/tya5/reyn/blob/main/CLAUDE.md) for the rule.

## Where ops are exposed to the LLM

The OS injects available ops into every context frame as `available_control_ops`. Each entry includes a `kind`, a one-line description, and a worked example. The LLM picks ops by matching its intent to descriptions — phase markdown MUST NOT describe op syntax (P8).

## See also

- [run.md](../cli/run.md) — `--allow-shell`, `--allow-untrusted-python`
- [events.md](events.md) — events emitted per op kind
- [Concepts: principles P8](../../concepts/principles.md#p8-phase-instructions-contain-only-domain-logic)
