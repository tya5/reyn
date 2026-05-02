---
type: concept
topic: architecture
audience: [human, agent]
---

# Product Think

The agent-as-a-product perspective: how it feels to use, what it costs to run, how predictable it is in the wild. Easy to under-invest in because it's not a research problem — but it's what determines whether anyone keeps the system around.

## How reyn handles it

### CLI affordances

The reyn CLI is structured as small, composable subcommands rather than one monolithic entrypoint:

| Command | Purpose |
|---------|---------|
| `reyn run` | Run a skill end-to-end |
| `reyn eval` | Run an eval spec |
| `reyn lint` | Lint a skill (graph, frontmatter, Python AST) |
| `reyn chat` | Interactive REPL with router + memory |
| `reyn init` | Scaffold `reyn.yaml` and `.reyn/` |
| `reyn skills` | List available skills, show one |
| `reyn permissions` | Inspect / revoke saved approvals |
| `reyn memory` | List / show / edit / search / export memory |
| `reyn events` | Replay a saved event log |
| `reyn config` | View / edit configuration |

Each one can be learned in isolation; they compose by sharing the same `reyn.yaml` and `.reyn/` state directory.

### Cost discipline

Three levers, all surfaced as flags or config:

- **Model classes (`light` / `standard` / `strong`).** A skill is written without naming a specific model; the resolver maps the class to a concrete LiteLLM model string from `reyn.yaml`. Switching cost tiers per project (or per run with `--model`) is a one-line change. Eval can run on `light` during iteration and `strong` for final grading.
- **Per-run cost reporting.** `reyn run` and `reyn eval` print token usage and USD cost on the final line. Eval reports persist per-case cost so cost regressions show up in the same place quality regressions do.
- **`limits.phase.max_visits` and `limits.phase.max_wall_seconds`.** Cap runaway loops and per-phase time budgets — both are cost ceilings (each visit is at least one LLM call, and time-bounded phases prevent slow-LLM blowups).

### Predictable UX

A few small choices that compound:

- **`output_language`.** One config key controls the language of user-facing output across every skill. No per-skill localization code.
- **`--events` / `--conversation`.** When a run does something unexpected, the artifact-of-record is one CLI call away.
- **State is on disk.** `.reyn/` holds events, chats, eval reports, approvals, memory. Nothing important is in process memory only.

### Composition without programming

The system rewards thinking in skills rather than functions. `chat` is a router skill; eval is a skill that iterates a judge skill; importer/improver/builder are themselves skills. New high-level capabilities tend to be new skills rather than new CLI subcommands.

## Where it's still thin

A handful of UX/cost levers are missing or thin:

- **No streaming output.** A long-running phase shows nothing on the console until it completes (the event log fills in real time, but the rendered output is per-phase). For interactive work this is OK; for very long-running skills, it's not.
- **No cost dashboard or trend view.** Per-run cost is shown; aggregating across runs is the user's job (the data is structured enough to feed into other tools).
- **Onboarding has rough edges.** `reyn init` scaffolds config but tutorial 01 is the actual orientation; a single integrated `reyn quickstart` doesn't exist.

These are addressable without changing the OS — they're product polish on top of an already-stable runtime.

## See also

- [Reference: cli/run](../../reference/cli/run.md)
- [Reference: cli/eval](../../reference/cli/eval.md)
- [Reference: cli/chat](../../reference/cli/chat.md)
- [Reference: cli/common-flags](../../reference/cli/common-flags.md)
- [How-to: localize output](../../how-to/localize-output.md)
- [evaluation-and-observability.md](evaluation-and-observability.md) — what cost data is collected
- [retrieval-engineering.md](retrieval-engineering.md) — chat memory affects UX directly
