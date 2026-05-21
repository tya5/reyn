# Popular local MCP servers — install + usage procedure

A copy-paste-runnable procedure for 5 popular local MCP servers with
Reyn. Each section shows:

1. **Install** — one `reyn mcp install` command. Post-PR #331 the
   install produces a loader-ready config with no manual edits.
2. **Direct smoke** — connectivity / tool-discovery / one tool call
   via the `scripts/mcp_smoke.py` runner. Useful for "is the server
   alive".
3. **Usage from chat** — a real `reyn chat` conversation that
   exercises the server through the chat router. Post-PR #342 the
   router signals catalog partiality so the LLM proactively calls
   `list_actions(filter='<server>')` for capabilities it doesn't see
   in its hot-list.

Servers covered:

- [time](#time) — timezone-aware current time / conversion
- [git](#git) — local repo operations (log / status / diff / branch)
- [sequential-thinking](#sequential-thinking) — chain-of-thought scratchpad
- [sqlite](#sqlite) — local DB queries (read + write + schema)
- [everything](#everything) — demo kitchen-sink covering protocol primitives

> **Why no filesystem / memory / fetch section?** All three MCP servers
> structurally overlap with Reyn built-in ops:
>
> - filesystem ↔ `file__*` (= read / write / list / grep / glob)
> - memory ↔ `memory.operation__*` (= remember_shared / forget)
> - fetch ↔ `web__fetch` (= HTTP fetch with markdown extraction)
>
> With both available, the chat router consistently picks the
> Reyn-internal op on natural prompts (10/10 in measurement,
> 2026-05-21). The MCP servers don't get exercised through the
> agent path.
>
> **Extraction parity (post-#355)**: install `pip install reyn[fetch]`
> to add trafilatura as the `web__fetch` HTML extractor — at that
> point, the Reyn op matches `mcp-server-fetch`'s extraction quality
> for content-dense pages. The MCP server's remaining advantages are
> `start_index` pagination (= tracked in #357) and robots.txt
> awareness. Use direct calls (`scripts/mcp_smoke.py`) or the MCP
> server itself only if you specifically need those.

> **Chat-history pollution caveat (= issue #352).** If your agent
> has previously refused a capability (= the LLM said "I cannot
> ..."), in-context learning may continue the refusal pattern on
> subsequent turns even though the SP signal directs otherwise. If
> the usage example below isn't producing the expected tool call,
> clear the agent's history first:
>
> ```bash
> echo -n > .reyn/agents/default/history.jsonl
> ```
>
> Fresh-history clean-state success rate for the natural-prompt
> usage examples below is ~90% (measured 2026-05-21 against
> `gemini-2.5-flash-lite`). Polluted-history rate degrades sharply.
> See [#352](https://github.com/tya5/reyn/issues/352) for the
> structural mitigation discussion.

## Prerequisites

- `node` + `npx` (most servers are npm packages)
- `uv` + `uvx` (= Python servers; `brew install uv`)
- Reyn installed with the `[mcp]` extra: `pip install -e ".[mcp]"`
- For chat usage: pre-approve the per-server permission once per
  server (= one-liner shown in each section).

The smoke runner `scripts/mcp_smoke.py` goes straight to
`reyn.mcp_client.MCPClient`, bypassing the chat router. Useful for
connectivity sanity. For agent-driven usage (= the typical end-user
shape), the chat router calls the server via the universal
`call_mcp_tool` / `invoke_action` dispatch.

---

## time

Timezone-aware time queries. Python-based (uvx), so requires `uv`.

### Prerequisite

```bash
brew install uv
```

### Install

```bash
reyn mcp install --source pypi:mcp-server-time --non-interactive
```

### Direct smoke

```bash
python scripts/mcp_smoke.py time get_current_time '{"timezone": "Asia/Tokyo"}'
```

Expected: `content[0].text` carries `{"timezone": "Asia/Tokyo", "datetime": "<ISO 8601>", ...}`.

### Usage from chat

```bash
echo 'mcp.time: true' >> .reyn/approvals.yaml

reyn chat
> What time is it in Tokyo right now?
```

The agent calls `mcp.tool__time.get_current_time` and replies in
natural language. For multi-timezone queries ("Tokyo, NYC, London"),
the agent chains 3 calls and synthesises one answer.

### Tools surfaced

`get_current_time` / `convert_time`.

---

## git

Local git repo operations via `mcp-server-git` (Python / uvx).

### Install

```bash
reyn mcp install --source pypi:mcp-server-git --non-interactive
```

### Direct smoke

```bash
python scripts/mcp_smoke.py git git_log "{\"repo_path\": \"$PWD\", \"max_count\": 3}"
python scripts/mcp_smoke.py git git_branch "{\"repo_path\": \"$PWD\", \"branch_type\": \"local\"}"
```

Expected: 3 most-recent commits / local branches listed.

### Usage from chat

```bash
echo 'mcp.git: true' >> .reyn/approvals.yaml

reyn chat
> Summarise the last 3 commits in this repo.
```

The agent calls `mcp.tool__git.git_log` with `repo_path` set to the
session's working directory and produces a short summary.

### Tools surfaced

`git_status` / `git_diff_unstaged` / `git_diff_staged` / `git_diff` /
`git_commit` / `git_add` / `git_reset` / `git_log` /
`git_create_branch` / `git_checkout` / `git_show` / `git_branch`.

---

## sequential-thinking

A meta tool for guided chain-of-thought reasoning. Useful as a demo
of MCP servers that wrap *workflow patterns* rather than I/O.

### Install

```bash
reyn mcp install --source npm:@modelcontextprotocol/server-sequential-thinking --non-interactive
```

### Direct smoke

```bash
python scripts/mcp_smoke.py sequential-thinking sequentialthinking '{
  "thought": "Verify the smoke harness works for stateful tools.",
  "thoughtNumber": 1,
  "totalThoughts": 1,
  "nextThoughtNeeded": false
}'
```

Expected: `structuredContent` carries `{"thoughtNumber": 1, ...}`.

### Usage from chat

```bash
echo 'mcp.sequential-thinking: true' >> .reyn/approvals.yaml

reyn chat
> Use sequential-thinking to plan how to organise a personal task list.
```

The agent emits a series of `mcp.tool__sequential_thinking.sequentialthinking`
calls (typically 5-7 thoughts) and synthesises the chain into a
natural-language plan. The server tracks the thought history
internally; multiple calls build up a chain inside one server
lifetime.

> Note: the keyword "sequential-thinking" in the user prompt helps
> the router pick this server over generic problem-solving paths
> (= invoke_action with no clear target).

### Tools surfaced

`sequentialthinking` (single tool — chained calls build up the
thought sequence).

---

## sqlite

Local SQLite database via `mcp-server-sqlite` (Python / uvx).

### Install

```bash
mkdir -p ./.mcp-sandbox && rm -f ./.mcp-sandbox/test.db

reyn mcp install --source pypi:mcp-server-sqlite \
    --args "--db-path $PWD/.mcp-sandbox/test.db" --non-interactive
```

### Direct smoke

```bash
python scripts/mcp_smoke.py sqlite create_table \
    '{"query": "CREATE TABLE smoke (id INTEGER PRIMARY KEY, msg TEXT)"}'
python scripts/mcp_smoke.py sqlite write_query \
    '{"query": "INSERT INTO smoke (msg) VALUES (\"hello from sqlite mcp\")"}'
python scripts/mcp_smoke.py sqlite read_query \
    '{"query": "SELECT * FROM smoke"}'
```

Expected: third call returns `[{'id': 1, 'msg': 'hello from sqlite mcp'}]`.

### Usage from chat

```bash
echo 'mcp.sqlite: true' >> .reyn/approvals.yaml

# (Optional) ensure clean history if you've previously interacted with sqlite:
echo -n > .reyn/agents/default/history.jsonl

reyn chat
> Create a `notes` table in sqlite with columns id and body, then
> insert a row with body "first note", and show me everything in the table.
```

The agent chains three `mcp.tool__sqlite.*` calls (= `create_table` →
`write_query` → `read_query`) within one turn. Post-PR #342 success
rate ≈ 90% on clean history; if the agent says "I cannot list
tables...", wipe the history (line above) and retry — see #352 for
why.

### Tools surfaced

`read_query` / `write_query` / `create_table` / `list_tables` /
`describe_table` / `append_insight`.

---

## everything

Demo "kitchen sink" server covering most MCP protocol primitives.

### Install

```bash
reyn mcp install --source npm:@modelcontextprotocol/server-everything --non-interactive
```

### Direct smoke

```bash
python scripts/mcp_smoke.py everything
python scripts/mcp_smoke.py everything get-sum '{"a": 17, "b": 25}'
python scripts/mcp_smoke.py everything echo '{"message": "hello"}'
```

Expected: 13 tools listed; sum returns "The sum of 17 and 25 is 42.".

### Usage from chat

```bash
echo 'mcp.everything: true' >> .reyn/approvals.yaml

# Optional: clean history (= same caveat as sqlite)
echo -n > .reyn/agents/default/history.jsonl

reyn chat
> Use the everything MCP server to compute 17 plus 25.
```

The agent calls `mcp.tool__everything.get-sum` with `{a: 17, b: 25}`
and reports the result. Post-PR #342 success rate ≈ 90% on clean
history.

> Note: explicitly mentioning "the everything MCP server" in the
> prompt helps the router disambiguate; with a generic "compute 17
> plus 25" the LLM may answer arithmetically without tool calls.

### Tools surfaced

`echo` / `get-sum` / `get-env` / `get-tiny-image` /
`get-annotated-message` / `get-structured-content` /
`get-resource-links` / `get-resource-reference` /
`gzip-file-as-resource` / `toggle-simulated-logging` /
`toggle-subscriber-updates` / `trigger-long-running-operation` /
`simulate-research-query`.

`trigger-long-running-operation` is especially useful for testing
[PR #266](https://github.com/tya5/reyn/pull/266)'s MCP progress
callback wire — it emits `notifications/progress` during execution.

---

## Adding more servers

For any stdio-transport MCP server:

```bash
reyn mcp install --source npm:<package>           # or pypi:<package>
echo "mcp.<server-name>: true" >> .reyn/approvals.yaml
reyn chat
```

The install command writes a loader-ready config automatically (= PR
\#331 fixed the install-flow UX issues #318 / #319 / #320). Servers
requiring credentials: `reyn mcp set-secret <name> <KEY>` + reference
`${KEY}` in the YAML `env:` block — see
[Reference: `reyn.yaml` § MCP servers](../../reference/config/reyn-yaml.md#mcp-servers).

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| Agent says "I cannot ..." even though the server is installed | History pollution (= prior refusal turn) | `echo -n > .reyn/agents/<name>/history.jsonl` then retry. See [#352](https://github.com/tya5/reyn/issues/352) |
| `MCP server <name> access denied` | Permission not pre-approved | `echo 'mcp.<name>: true' >> .reyn/approvals.yaml` |
| `not found` errors after install | Server uses uvx (Python) but `uv` not installed | `brew install uv` |
| Server config in YAML missing `type: stdio` or has `server-` prefix | Pre-PR #331 install path | Re-install via `reyn mcp install` post-#331 |
| MCP fetch / filesystem / memory installed but agent uses Reyn op instead | Reyn internal op (`web__fetch` / `file__*` / `memory.operation__*`) wins on natural prompts | Use `scripts/mcp_smoke.py` direct call; the MCP server isn't exercised through the chat router |
