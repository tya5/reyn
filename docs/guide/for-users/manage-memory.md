---
type: how-to
topic: using-reyn
audience: [human]
---

# Inspect and manage memory

Reyn remembers facts across sessions automatically — you don't have to set
anything up. This page is for when you want to *look at* or *curate* what it
has stored: review a fact, fix a stale one, back memories up, or move them to
another machine. All of this is done with `reyn memory` while no chat session
is running.

## Where memories live

| Layer | Path | `--agent` flag |
|-------|------|----------------|
| Shared (default) | `.reyn/memory/` | omit |
| Per-agent | `.reyn/agents/<name>/memory/` | `--agent <name>` |

Every command below defaults to the shared layer; add `--agent <name>` to
target one agent's memories instead.

## Look at what's stored

```bash
reyn memory list                    # all memory files (shared layer)
reyn memory list --agent my_agent   # one agent's memories
reyn memory show preferences        # print one memory's contents
reyn memory search "API key" -i     # regex search (-i = case-insensitive)
```

## Fix or remove a memory

```bash
reyn memory edit preferences        # open in $EDITOR
reyn memory delete preferences      # delete (prompts to confirm)
reyn memory delete preferences -y   # delete without the prompt
```

`delete` also removes the entry from the layer's `MEMORY.md` index, so the
index never points at a file that no longer exists.

## Back up and restore

```bash
reyn memory export --out backup.json      # dump all memories to a file
reyn memory export                        # or to stdout (default)
reyn memory import backup.json            # restore (skips existing files)
reyn memory import backup.json --overwrite # restore, replacing existing
```

`import` skips any memory that already exists unless you pass `--overwrite` —
so a plain import is safe to re-run.

## See also

- [Reference: `reyn memory`](../../reference/cli/memory.md) — every subcommand, flag, and exit code
- [Concepts: memory](../../concepts/data-retrieval/memory.md) — how Reyn decides what to remember and how recall works
