# Popular local MCP servers — smoke-test procedure

A copy-paste-runnable procedure for verifying popular MCP servers with
Reyn locally. Eight servers verified end-to-end (4 npm + 4 pypi/uvx):

- [filesystem](#filesystem) — sandboxed file read/write
- [memory](#memory) — knowledge-graph KV (persists across calls)
- [time](#time) — timezone-aware current time / conversion
- [everything](#everything) — demo kitchen-sink covering protocol primitives
- [sqlite](#sqlite) — local DB queries (read + write + schema)
- [git](#git) — local repo operations (log / status / diff / branch)
- [sequential-thinking](#sequential-thinking) — chain-of-thought scratchpad
- [fetch](#fetch) — HTTP fetch with markdown extraction

Each section captures the install command, the workarounds needed
(see [Known issues](#known-issues) at the bottom), and a minimal
smoke test that confirms the server is reachable from Reyn's MCP
client. Verified 2026-05-20 against Reyn HEAD on macOS (Darwin 25.x).

## Prerequisites

- `node` + `npx` (most servers are npm packages)
- `uv` + `uvx` (= some servers are Python; install via `brew install uv`)
- Reyn installed with the `[mcp]` extra: `pip install -e ".[mcp]"`
- A reusable smoke runner: `scripts/mcp_smoke.py` (in-tree)

The smoke runner bypasses the skill / agent / permissions layer and
goes straight to `reyn.mcp_client.MCPClient`. Use it for connectivity
+ tool-discovery + invocation sanity. For full integration tests
(skill → permissions → mcp op → result) see the filesystem section.

```bash
# usage
python scripts/mcp_smoke.py <server-name>                       # list tools
python scripts/mcp_smoke.py <server-name> <tool> '<json-args>'  # call tool
```

---

## filesystem

Read / write files under a configured sandbox root. Most useful + safest local server.

### Install

```bash
reyn mcp install --source npm:@modelcontextprotocol/server-filesystem \
    --args "/Users/<you>/Workspace/<project>/.mcp-sandbox" \
    --non-interactive
```

**Apply workarounds** (see [#318](https://github.com/tya5/reyn/issues/318) + [#319](https://github.com/tya5/reyn/issues/319) + [#320](https://github.com/tya5/reyn/issues/320)):

```python
# Add type: stdio + rename to short name. Run from project root.
python - <<'PY'
import yaml
with open("reyn.local.yaml") as f: cfg = yaml.safe_load(f)
fs = cfg["mcp"]["servers"].pop("server-filesystem")
fs["type"] = "stdio"
cfg["mcp"]["servers"]["filesystem"] = {"type": fs.pop("type"), **fs}
with open("reyn.local.yaml", "w") as f: yaml.safe_dump(cfg, f, sort_keys=False)
PY
```

> **macOS users**: avoid `/tmp` as the sandbox root — it's a symlink to
> `/private/tmp` and the server compares literal paths. Use a path
> inside the project (e.g. `./.mcp-sandbox`) or your home directory.

### Pre-approve + smoke

```bash
mkdir -p .mcp-sandbox && echo "hello mcp" > .mcp-sandbox/x.txt
echo 'mcp.filesystem: true' >> .reyn/approvals.yaml

# Direct client smoke
python scripts/mcp_smoke.py filesystem read_text_file '{"path": ".mcp-sandbox/x.txt"}'

# End-to-end (skill → permissions → mcp op)
reyn run read_local_files '.mcp-sandbox/x.txt の内容を教えて'
```

Expected: agent returns the file content; events log shows
`act_executed` with `op_kinds: ["mcp"]` and `status: "ok"`.

---

## memory

Knowledge-graph KV server. Entities + observations + relations persist
across calls within one server lifetime (in-memory; restart resets).

### Install

```bash
reyn mcp install --source npm:@modelcontextprotocol/server-memory --non-interactive
```

Same workarounds as filesystem (#318 + #319):

```python
python - <<'PY'
import yaml
with open("reyn.local.yaml") as f: cfg = yaml.safe_load(f)
fs = cfg["mcp"]["servers"].pop("server-memory")
fs["type"] = "stdio"
cfg["mcp"]["servers"]["memory"] = {"type": fs.pop("type"), **fs}
with open("reyn.local.yaml", "w") as f: yaml.safe_dump(cfg, f, sort_keys=False)
PY
```

### Smoke

```bash
# List tools (= 9 KV / graph primitives)
python scripts/mcp_smoke.py memory

# Create an entity
python scripts/mcp_smoke.py memory create_entities '{
  "entities": [
    {"name": "Reyn", "entityType": "project",
     "observations": ["agent OS for LLM-driven workflows"]}
  ]
}'

# Search for it
python scripts/mcp_smoke.py memory search_nodes '{"query": "Reyn"}'
```

Expected: second call's `structuredContent.entities` contains the
"Reyn" entity created by the first call.

### Tools surfaced

`create_entities` / `create_relations` / `add_observations` /
`delete_*` / `read_graph` / `search_nodes` / `open_nodes`.

---

## time

Timezone-aware time queries. Python-based (uvx), so requires `uv`
installed first.

### Prerequisite

```bash
brew install uv     # adds uvx as the runtime
```

### Install

```bash
reyn mcp install --source pypi:mcp-server-time --non-interactive
```

> The npm registry has **no** `@modelcontextprotocol/server-time` —
> the official time server is a Python package at
> [pypi.org/project/mcp-server-time](https://pypi.org/project/mcp-server-time).
> Install via the `pypi:` source specifier.

Same workarounds (#318 + #319; the auto-derived name is
`mcp-server-time`):

```python
python - <<'PY'
import yaml
with open("reyn.local.yaml") as f: cfg = yaml.safe_load(f)
fs = cfg["mcp"]["servers"].pop("mcp-server-time")
fs["type"] = "stdio"
cfg["mcp"]["servers"]["time"] = {"type": fs.pop("type"), **fs}
with open("reyn.local.yaml", "w") as f: yaml.safe_dump(cfg, f, sort_keys=False)
PY
```

### Smoke

```bash
python scripts/mcp_smoke.py time get_current_time '{"timezone": "Asia/Tokyo"}'
```

Expected: `content[0].text` contains `{"timezone": "Asia/Tokyo",
"datetime": "<ISO 8601>", "day_of_week": "...", "is_dst": false}`.

### Tools surfaced

`get_current_time` / `convert_time`.

---

## everything

Demo "kitchen sink" server covering most MCP protocol primitives.
Useful for sanity-checking tool-discovery + structured-content +
long-running ops + sampling, all in one server.

### Install

```bash
reyn mcp install --source npm:@modelcontextprotocol/server-everything --non-interactive
```

Same workarounds (#318 + #319):

```python
python - <<'PY'
import yaml
with open("reyn.local.yaml") as f: cfg = yaml.safe_load(f)
fs = cfg["mcp"]["servers"].pop("server-everything")
fs["type"] = "stdio"
cfg["mcp"]["servers"]["everything"] = {"type": fs.pop("type"), **fs}
with open("reyn.local.yaml", "w") as f: yaml.safe_dump(cfg, f, sort_keys=False)
PY
```

### Smoke

```bash
# Tool discovery
python scripts/mcp_smoke.py everything

# Sum
python scripts/mcp_smoke.py everything get-sum '{"a": 17, "b": 25}'

# Echo
python scripts/mcp_smoke.py everything echo '{"message": "hello from reyn smoke"}'
```

Expected: `content[0].text` carries the computed sum / echoed message.

### Tools surfaced

`echo` / `get-sum` / `get-env` / `get-tiny-image` /
`get-annotated-message` / `get-structured-content` /
`get-resource-links` / `get-resource-reference` / `gzip-file-as-resource` /
`toggle-simulated-logging` / `toggle-subscriber-updates` /
`trigger-long-running-operation` / `simulate-research-query`.

`trigger-long-running-operation` is especially useful for testing
[PR #266](https://github.com/tya5/reyn/pull/266)'s MCP progress
callback wire — it emits `notifications/progress` during execution.

---

## sqlite

Local SQLite database via `mcp-server-sqlite` (Python / uvx). Supports
read (SELECT), write (INSERT / UPDATE / DELETE), schema (CREATE), and
table introspection.

### Prerequisite

```bash
brew install uv     # if not already installed
```

### Install

```bash
reyn mcp install --source pypi:mcp-server-sqlite --non-interactive
```

Same workarounds (#318 + #319); additionally pass the `--db-path` arg:

```python
python - <<'PY'
import yaml
with open("reyn.local.yaml") as f: cfg = yaml.safe_load(f)
fs = cfg["mcp"]["servers"].pop("mcp-server-sqlite")
fs["type"] = "stdio"
fs["args"] = fs.get("args", []) + ["--db-path", "./.mcp-sandbox/test.db"]
cfg["mcp"]["servers"]["sqlite"] = {"type": fs.pop("type"), **fs}
with open("reyn.local.yaml", "w") as f: yaml.safe_dump(cfg, f, sort_keys=False)
PY
```

### Smoke

```bash
mkdir -p .mcp-sandbox && rm -f .mcp-sandbox/test.db
python scripts/mcp_smoke.py sqlite create_table \
    '{"query": "CREATE TABLE smoke (id INTEGER PRIMARY KEY, msg TEXT)"}'
python scripts/mcp_smoke.py sqlite write_query \
    '{"query": "INSERT INTO smoke (msg) VALUES (\"hello from sqlite mcp\")"}'
python scripts/mcp_smoke.py sqlite read_query \
    '{"query": "SELECT * FROM smoke"}'
```

Expected: third call returns `[{'id': 1, 'msg': 'hello from sqlite mcp'}]`.

### Tools surfaced

`read_query` / `write_query` / `create_table` / `list_tables` /
`describe_table` / `append_insight`.

---

## git

Local git repo operations via `mcp-server-git` (Python / uvx). Useful
when an agent needs to inspect commit history / branches / diffs
during a development workflow.

### Install

```bash
reyn mcp install --source pypi:mcp-server-git --non-interactive
```

Same workarounds (#318 + #319):

```python
python - <<'PY'
import yaml
with open("reyn.local.yaml") as f: cfg = yaml.safe_load(f)
fs = cfg["mcp"]["servers"].pop("mcp-server-git")
fs["type"] = "stdio"
cfg["mcp"]["servers"]["git"] = {"type": fs.pop("type"), **fs}
with open("reyn.local.yaml", "w") as f: yaml.safe_dump(cfg, f, sort_keys=False)
PY
```

### Smoke

```bash
# Recent commits in current repo
python scripts/mcp_smoke.py git git_log \
    "{\"repo_path\": \"$PWD\", \"max_count\": 3}"

# Local branches
python scripts/mcp_smoke.py git git_branch \
    "{\"repo_path\": \"$PWD\", \"branch_type\": \"local\"}"
```

Expected: `git_log` returns "Commit history:\n..." with 3 most recent
entries; `git_branch` lists local branches with the current one marked.

### Tools surfaced

`git_status` / `git_diff_unstaged` / `git_diff_staged` / `git_diff` /
`git_commit` / `git_add` / `git_reset` / `git_log` /
`git_create_branch` / `git_checkout` / `git_show` / `git_branch`.

> Note: the git server uses `Repo(path)` so it works on any local
> repo, not just CWD. Pass `repo_path` as an absolute path for
> clarity.

---

## sequential-thinking

A meta tool for guided chain-of-thought reasoning. Useful as a demo
of MCP servers that wrap *workflow patterns* rather than I/O.

### Install

```bash
reyn mcp install --source npm:@modelcontextprotocol/server-sequential-thinking \
    --non-interactive
```

Same workarounds (#318 + #319):

```python
python - <<'PY'
import yaml
with open("reyn.local.yaml") as f: cfg = yaml.safe_load(f)
fs = cfg["mcp"]["servers"].pop("server-sequential-thinking")
fs["type"] = "stdio"
cfg["mcp"]["servers"]["sequential_thinking"] = {"type": fs.pop("type"), **fs}
with open("reyn.local.yaml", "w") as f: yaml.safe_dump(cfg, f, sort_keys=False)
PY
```

### Smoke

```bash
python scripts/mcp_smoke.py sequential_thinking sequentialthinking '{
  "thought": "Verify the smoke harness works for stateful tools.",
  "thoughtNumber": 1,
  "totalThoughts": 1,
  "nextThoughtNeeded": false
}'
```

Expected: `structuredContent` carries
`{"thoughtNumber": 1, "totalThoughts": 1, "nextThoughtNeeded": false,
"branches": [], "thoughtHistoryLength": 1}`.

### Tools surfaced

`sequentialthinking` (the single tool — multiple invocations build up
a thought chain inside the server's memory).

---

## fetch

HTTP fetch with markdown extraction via `mcp-server-fetch` (Anthropic
official, Python / uvx). Useful for agents that need to read web pages
as plain text.

### Install

```bash
reyn mcp install --source pypi:mcp-server-fetch --non-interactive
```

Same workarounds (#318 + #319):

```python
python - <<'PY'
import yaml
with open("reyn.local.yaml") as f: cfg = yaml.safe_load(f)
fs = cfg["mcp"]["servers"].pop("mcp-server-fetch")
fs["type"] = "stdio"
cfg["mcp"]["servers"]["fetch"] = {"type": fs.pop("type"), **fs}
with open("reyn.local.yaml", "w") as f: yaml.safe_dump(cfg, f, sort_keys=False)
PY
```

### Smoke

```bash
python scripts/mcp_smoke.py fetch fetch \
    '{"url": "https://example.com", "max_length": 500}'
```

Expected: `content[0].text` contains "Contents of https://example.com/:\n
This domain is for use in documentation examples...".

> First invocation may print a benign uvx-init stderr line that
> looks like a `pydantic_core.ValidationError` — this is uvx's
> install / dep-check output bleeding into the smoke runner's
> error stream. The tool call itself succeeds (= the `content`
> block is returned correctly). It does not recur on subsequent
> invocations once uvx has cached the package.

### Tools surfaced

`fetch` (the single tool — supports `url` / `max_length` /
`start_index` / `raw=true|false` args).

### Comparison vs Reyn's built-in `web_fetch` op

Reyn already ships a `web_fetch` Control IR op
(`src/reyn/op_runtime/web_fetch.py`). Differences:

| | `web_fetch` op | `fetch` MCP server |
|---|---|---|
| Transport | In-process (httpx) | stdio subprocess (uvx) |
| Latency | ~50ms cold | ~200ms cold (subprocess spawn) |
| Markdown extraction | Reyn's own | trafilatura (= upstream choice) |
| Pagination | `max_length` only | `max_length` + `start_index` |
| Permission gate | `web_fetch` | `mcp.fetch` |

For typical skills, the built-in `web_fetch` op is cheaper. Use the
MCP `fetch` server when you specifically want trafilatura's extraction
quality or `start_index` pagination.

---

## Known issues

Encountered during this smoke-test round (2026-05-20):

| Issue | Symptom | Workaround |
|---|---|---|
| [#318](https://github.com/tya5/reyn/issues/318) | `reyn mcp install` omits `type: stdio` → loader fails with `Unsupported MCP server type: None` | Add `type: stdio` to the server entry post-install (see snippets above) |
| [#319](https://github.com/tya5/reyn/issues/319) | Auto-derived server name keeps `server-` / `mcp-server-` prefix → stdlib skill expecting short name (e.g. `filesystem`) fails to find it | Rename the YAML key to the short form post-install |
| [#320](https://github.com/tya5/reyn/issues/320) | macOS `/tmp` symlink resolves to `/private/tmp` → server's literal path check denies `read_text_file` | Use a sandbox path outside `/tmp` (= project dir or home dir) |

The Python snippet in each section bakes in the #318 + #319
workarounds. #320 affects only the filesystem server's sandbox root.

---

## Adding more servers

The same procedure works for any stdio-transport MCP server:

1. `reyn mcp install --source npm:<pkg>` (or `pypi:<pkg>` for Python)
2. Apply the #318 + #319 workaround Python snippet (= add `type: stdio`,
   rename to a stable short key)
3. `python scripts/mcp_smoke.py <name>` to list tools
4. `python scripts/mcp_smoke.py <name> <tool> '<args>'` to verify a call

If the server requires credentials, use `reyn mcp set-secret <name>
<KEY>` and reference the secret in the YAML's `env:` block (see
`docs/guide/for-skill-authors/use-an-mcp-server.ja.md` for the full
secret-handling shape).

When all 3 install-flow issues (#318 / #319 / #320) close, the
procedure shrinks to: `reyn mcp install ...` + `python scripts/mcp_smoke.py ...`.
