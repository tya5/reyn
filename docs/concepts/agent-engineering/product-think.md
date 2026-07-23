---
type: concept
topic: architecture
audience: [human, agent]
---

# Product Think

The agent-as-a-product perspective: how it feels to use, what it costs to run, how predictable it is in the wild. Easy to under-invest in because it's not a research problem — but it's what determines whether anyone keeps the system around.

## How reyn handles it

### CLI affordances

reyn's CLI is structured as small, composable subcommands rather than one monolithic entrypoint — each owning exactly its own subsystem's operator surface (agent / topology / memory / permissions / events / mcp / config / …), sharing the same `reyn.yaml` and `.reyn/` state directory rather than a shared mega-command. See [feature-map.md's CLI section](../../feature-map.md#cli) for the full, current command inventory rather than a duplicated list here.

### Live legibility: the inline CUI's audit chips

The inline CUI's status-chip bar (Agents / Cost / Model / Tools / MCP / Skills / Hooks / Pipes / Cron / Tasks) surfaces the same operator-visible state the P6 audit-event log records, live and inline rather than only available via after-the-fact replay — this is the dual-facet companion to the Observability lens's reading of the same chips (see [observability.md](observability.md)).

### Cost reporting and reduction — distinct from bounding

Two things that look similar but are lens-distinct:

- **Cost *reporting* (this lens)**: `/cost` gives a quick token + USD summary for the current agent; `/budget` gives a full breakdown. `cost_warn` is a pre-selection warning to the operator when the resolved model's cost-per-1M-tokens exceeds a threshold, de-duped once per model per session — legibility and predictability, nothing more.
- **Cost *bounding* (the cross-cutting band's `cost/budget` member, not this lens)**: hard per-agent / daily / monthly token+USD caps that refuse further spend once exceeded. Don't cite the bounding caps as a Product Think exemplar — they're the band's job, not this lens's (see `CLAUDE.md`'s Constitution section for the full band↔lens distinction).
- **Cost *reduction*** is this lens's other facet: `present` routes bulk data to the surface at ~0 output tokens instead of reproducing it as LLM output — a genuine token-cost reduction, not just a reporting mechanism.

### Predictable UX

- **`output_language`.** One config key controls the language of user-facing output. No per-agent localization code.
- **`reyn events`.** When a run does something unexpected, the artifact-of-record is one CLI call away.
- **State is on disk.** `.reyn/` holds events, chats, approvals, memory. Nothing important is in process memory only.
- **On-limit modes.** `interactive` / `auto_extend` / `unattended` give the operator predictable, config-selectable control over every loop/timeout/budget checkpoint uniformly — see [reliability-engineering.md](reliability-engineering.md).
- **`/agents` view.** Lists running agents/sessions and lets you attach — operator legibility into orchestrated work, spanning skill runs and delegated peers.

## Where it's still thin

- **No cost dashboard or trend view.** Per-run cost is shown (`/cost`, `/budget`); aggregating across runs is the operator's own job (the data is structured enough to feed into other tools).
- **Onboarding has rough edges.** `reyn init` scaffolds config, but the getting-started guide is the actual orientation — a single integrated one-command quickstart doesn't exist.

These are addressable without changing the OS — they're product polish on top of an already-stable runtime.

## See also

- `CLAUDE.md` (§ Constitution) — the Product Think lens's pass-line, and the bounding≠reduction/legibility distinction this page depends on
- [`docs/concepts/architecture/charter.md`](../architecture/charter.md) — the Product Think row, grounded across all 7 feature families
- [observability.md](observability.md) — the audit-chips dual-facet companion to this page's "Live legibility" section
- [Reference: cli/chat](../../reference/cli/chat.md)
- [Reference: config/budget](../../reference/config/budget.md)
