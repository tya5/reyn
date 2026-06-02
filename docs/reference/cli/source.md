---
type: reference
topic: cli
audience: [human, agent]
applies_to: [reyn source]
---

# `reyn source`

Manage indexed sources — the named document collections produced by `reyn run index_docs`. See [Concepts: RAG](../../concepts/data-retrieval/rag.md) for the mental model and indexing workflow.

## Synopsis

```
reyn source list   [--json]
reyn source describe <NAME>
reyn source rm     <NAME> [-y]
```

## Description

`reyn source` is the primary interface for inspecting and removing the indexed sources stored in `.reyn/index/`. Sources are created by running `reyn run index_docs`; this command group does not create or update them.

---

## Subcommands

### `list`

List all indexed sources registered in `.reyn/index/sources.yaml`.

```
reyn source list [--json]
```

**Description:** Displays each source name with its description, chunk count, embedding model, and last-indexed timestamp. If no sources are indexed, prints a getting-started hint.

**Options:**

| Flag | Description |
|------|-------------|
| `--json` | Output as a JSON array instead of the default table. Each element includes `name`, `description`, `path`, `chunk_count`, `embedding_model`, `last_indexed`, and `backend`. |

**Examples:**

```bash
reyn source list
```

Output:

```
NAME          DESCRIPTION                              CHUNKS  MODEL                    LAST INDEXED
────────────────────────────────────────────────────────────────────────────────────────────────────
memory        User notes / past session memos          142     text-embedding-3-small   2026-05-09T10:14:00Z
my_docs       Project documentation                    89      text-embedding-3-small   2026-05-10T08:30:00Z
reyn_code     Reyn Python framework source code        1247    text-embedding-3-small   2026-05-10T08:45:00Z
```

If no sources are indexed:

```
No indexed sources yet.
Try: reyn run index_docs --source <name> --path "<glob>" --description "<description>"
```

```bash
# Machine-readable output
reyn source list --json
```

**Exit codes:**

| Code | Meaning |
|------|---------|
| `0` | Success (including the empty-sources case). |
| `1` | I/O error reading `sources.yaml`. |

---

### `describe <NAME>`

Show detailed information for a single indexed source.

```
reyn source describe <NAME>
```

**Description:** Prints the full metadata for one source: name, description, path glob, chunk count, embedding model, storage backend, storage path, and last-indexed timestamp.

**Examples:**

```bash
reyn source describe my_docs
```

Output:

```
Source: my_docs
  Description:     Project documentation
  Path:            docs/**/*.md
  Chunks:          89
  Embedding model: text-embedding-3-small
  Backend:         sqlite
  Index path:      .reyn/index/my_docs/index.db
  Last indexed:    2026-05-10T08:30:00Z
```

**Exit codes:**

| Code | Meaning |
|------|---------|
| `0` | Source found and described. |
| `1` | Source not found (`<NAME>` is not in `sources.yaml`). |

---

### `rm <NAME>`

Remove an indexed source and all its associated index data.

```
reyn source rm <NAME> [-y]
```

**Description:** Drops the source's vector index from disk (`.reyn/index/<name>/index.db`) and removes the corresponding entry from `.reyn/index/sources.yaml`. The source will no longer appear in `reyn source list`, and the LLM will no longer see it in the system prompt.

By default, prompts for confirmation before deleting. Use `-y` to skip the prompt.

Internally invokes the `index_drop` op, which requires the `permissions.index_drop` permission (default: `ask`). On first run, reyn will prompt once for this permission unless it has been pre-approved.

**Options:**

| Flag | Description |
|------|-------------|
| `-y`, `--yes` | Skip the confirmation prompt. Useful in scripts or when iterating on indexing strategies. |

**Examples:**

```bash
# Interactive — shows a confirmation prompt
reyn source rm my_docs
```

Output:

```
Remove source 'my_docs' and delete .reyn/index/my_docs/index.db? [y/N] y
Source 'my_docs' removed.
```

```bash
# Skip the prompt
reyn source rm my_docs -y
```

```bash
# Typical iteration workflow: drop and re-index with a different strategy
reyn source rm my_docs -y
reyn run index_docs --source my_docs --path "docs/**/*.md" --description "Project documentation"
```

**Exit codes:**

| Code | Meaning |
|------|---------|
| `0` | Source removed successfully. |
| `1` | Source not found, or I/O error removing the index file or updating `sources.yaml`. |

---

## See also

- [Concepts: RAG](../../concepts/data-retrieval/rag.md) — indexing workflow, source model, and chunker overview
- [`reyn run index_docs`](run.md) — create or update a source index
- [`recall` tool](../../concepts/data-retrieval/rag.md) — LLM-facing retrieval tool
- [Concepts: permission model](../../concepts/runtime/permission-model.md) — `index_drop` permission gate
