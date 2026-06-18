---
type: reference
topic: cli
audience: [human, agent]
applies_to: [reyn run-once]
---

# `reyn run-once`

One-shot, non-interactive agent invocation. Reads the **whole** of stdin as a
single user message, drives the general agent to completion (any number of
tool-call iterations → one final stop), prints the final reply, and exits. It is
the batch / programmatic counterpart to interactive [`reyn chat`](chat.md) — the
SWE-bench runner and other automation pipe a whole task in as one message.

## Synopsis

```
reyn run-once [agent_name] [OPTIONS] < prompt
```

The prompt is read from stdin in full (not line by line).

## Positional arguments

| Name | Description |
|------|-------------|
| `agent_name` | Agent to drive. Default: `default`. |

## Options

| Flag | Description |
|------|-------------|
| `--max-iterations N` | Per-message tool-call budget for the autonomous loop. Default `80` — higher than interactive chat so the agent can iterate explore → edit → verify to completion. |
| `--grant-file-write` | Grant `file.read` + `file.write` at the resolver layer so the non-interactive agent edits its working tree without a prompt (bounded by the sandbox write-paths). Same as `reyn chat --grant-file-write`. |
| `--exclude-tools NAMES` | Comma-separated tool names to hide from the agent's LLM-visible catalog (e.g. `web__search,web__fetch`). Same as `reyn chat --exclude-tools`. |
| `--exclude-categories NAMES` | Comma-separated catalog category names to hide at the catalog source (e.g. `reyn_source` when the agent's own source is irrelevant to the task). Same as `reyn chat --exclude-categories`. |

Environment-backend flags and the [common flags](common-flags.md) are shared with
`reyn chat` / `reyn run`.

## Behavior notes

- **Stateless.** A one-shot run does **not** load the agent's persisted
  conversation history — there is no prior conversation to continue. The scoped
  session (permission grants, excluded tools, environment backend) is constructed
  exactly as for `reyn chat`; only the final drive differs (one-shot completion
  instead of the line-by-line REPL).
- **Whole-stdin read.** The entire stdin stream is taken as one message, so a
  multi-line task is delivered intact.

## Examples

Run the default agent on a piped prompt:

```bash
echo "Summarize the README and list open TODOs" | reyn run-once
```

Drive a named agent that may edit its working tree:

```bash
cat task.md | reyn run-once coder --grant-file-write
```

Hide web tools and the Reyn-source category for an external-repo task:

```bash
cat task.md | reyn run-once --exclude-tools web__search,web__fetch --exclude-categories reyn_source
```

## See also

- [`reyn chat`](chat.md) — the interactive counterpart (shares the scoped-session construction)
- [`reyn run`](run.md) — run a specific skill end-to-end
- [Common flags](common-flags.md)
