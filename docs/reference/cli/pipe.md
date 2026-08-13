---
type: reference
topic: cli
audience: [human, agent]
applies_to: [reyn pipe]
---

# `reyn pipe`

Manage and run registered pipelines directly, **outside a live chat session**.
Mirrors [`reyn mcp`](mcp.md)'s conventions closely: the same `register(sub)`/
`run_*(args)` shape, the same `--project` root resolution, and (for `install`) the
same permission/event-log bridging pattern `mcp install` uses.

## Synopsis

```
reyn pipe list
reyn pipe install [--path <file.yaml>] [--source <url>] [--name <name>] [--project <path>] [--non-interactive]
reyn pipe run <name> [--input <json>] [--project <path>]
```

## Subcommands

### `list`

Show every configured pipeline (`pipelines.entries`) with a **LOAD STATUS** column.
Unlike `mcp list`, there is no live-server handshake concept — loading IS the check,
so this always builds a real `PipelineRegistry` from the same merged
`pipelines.entries` cascade every session uses.

```bash
$ reyn pipe list

NAME                 PATH                  DESCRIPTION           ENABLED  LOAD STATUS
──────────────────────────────────────────────────────────────────────────────────────
research.summarize   pipelines/research    Summarize a topic     yes      loaded
legacy                                     (unused)               no      disabled
broken                broken.yaml                                yes      FAILED
```

The `NAME` column shows the **actual registered, runnable name** — the
`{key}.{declared-name}` form `reyn pipe run` accepts — never the bare entry key, for
a healthy entry. An entry registering more than one pipeline (a DSL file with
siblings, or a directory entry) gets one row per runnable name. A `FAILED` entry
(malformed DSL, unreadable path, a reserved `.` in the key) shows the bare entry key
instead, since it registered nothing runnable — `reyn pipe list` is the first-class
way to see load failures without digging through trace/logs.

### `install`

Install a pipeline into `reyn.yaml`/`.reyn/config/pipelines.yaml`, from a local DSL
file or a git/GitHub URL.

| Flag | Notes |
|---|---|
| `--path PATH` | Local pipeline DSL `*.yaml`. Required unless `--source` is given; with `--source`, selects the DSL file inside the cloned repo (only needed if there's more than one candidate). |
| `--source SOURCE` | Install from a git/GitHub URL, cloned to `.reyn/pipelines/<name>/`. Supports a `//` subdir suffix (`https://github.com/user/repo//pipelines/my-pipeline`). |
| `--name NAME` | Namespace key for the entry (default: the DSL file stem, or the source basename). Every pipeline in the file registers as `<name>.<declared-pipeline-name>`. Must not contain `.`. |
| `--project PATH` | Project root containing `reyn.yaml`. Defaults to the closest ancestor with one, or cwd. |
| `--non-interactive` | Suppress interactive prompts (for CI use). |

```bash
reyn pipe install --path ./my-pipeline.yaml
reyn pipe install --source 'https://github.com/user/repo//pipelines/research' --non-interactive
```

### `run <name>`

Execute a registered pipeline to completion and print its final result as JSON.

| Arg / Flag | Notes |
|---|---|
| `NAME` | The registered pipeline's fully-qualified `<entry-key>.<declared-name>` form (as shown by `reyn pipe list`). A bare `<entry-key>` also resolves automatically when it unambiguously matches exactly one registered pipeline under that key. |
| `--input JSON` | A JSON object string seeding the run's named stores (`ctx.*`, the first step's context). Default `"{}"`. |
| `--project PATH` | Same resolution as `install`. |

```bash
$ reyn pipe run research.summarize --input '{"topic": "reyn's own present layer"}'
{
  "pipe_data": "...",
  "named_stores": {"...": "..."}
}
```

**Every step kind runs standalone** (`transform`/`tool`/`agent`/`call`/`match`/
`fold`/`for_each`/`parallel`) — a `tool:` step dispatches through a real,
standalone `ToolContext`; an `agent:` step spawns a real ephemeral session under
the `default` agent identity via a real `AgentRegistry`. No live chat session or
router loop is needed.

**Permissions are fail-closed by default** — byte-identical to `reyn chat`'s own
no-flag posture. There is no per-invocation CLI grant flag (removed, #3924: hard to
scope safely in a multi-agent system); an operator opts a project into
`file.read`/`file.write` durably via `permissions.file.write: ["<zone-root>"]` in
`reyn.yaml`. `http.get` is never blanket-granted. This matters because a pipeline
may itself be installed from an untrusted source (`reyn pipe install --source`).

**Not resumable on process crash** — `reyn pipe run` is a one-shot, foreground,
single-process CLI command, deliberately NOT the live-session `run_pipeline` tool's
crash-recoverable driver-session path. If the process dies mid-run, that's the same
as any other CLI command dying mid-run: no "resume on the next chat turn" behavior
to expect. `--async` is explicitly rejected (exits 1 with an explanatory message) —
there is no fire-and-forget semantics for a foreground CLI invocation.

## Related

- [`reyn mcp`](mcp.md) — the sibling command `reyn pipe`'s conventions mirror
- [`reyn agent`](agent.md) — spawning agents inside a live session, vs. `pipe run`'s
  standalone ephemeral-session path for `agent:` steps
