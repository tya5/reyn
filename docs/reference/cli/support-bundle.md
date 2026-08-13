---
type: reference
topic: cli
audience: [human, agent]
applies_to: [reyn support-bundle]
---

# `reyn support-bundle`

Assemble a **redacted** diagnostic bundle (#1833) — a single zip an operator can
hand to support without leaking secrets. Collects the three observability
artifacts reyn already writes (LLM payload trace, WAL, event logs), filters by
session/time window, redacts every line through the existing secret-redaction
layer, and packs the result plus a secrets-free `meta.json`. No new redaction
logic and no provider calls — this command is the missing *assembly* +
*redaction-at-the-exit*, not a new diagnostic mechanism.

## Synopsis

```
reyn support-bundle [--session <id>] [--since <iso|Nd|Nh|Nm>] [-o <path>]
```

## Flags

| Flag | Notes |
|---|---|
| `--session ID` | Only include records whose `session`/`session_id`/`run_id`/`agent_id`/`agent`/`chain_id` field matches. Best-effort: a record with none of those fields is still included (favors completeness for diagnostics — redaction handles safety regardless of what's included). |
| `--since ISO\|Nd\|Nh\|Nm` | Only include records at/after this time. Accepts ISO-8601 or a relative window (`7d`, `12h`, `30m`). An overflowing relative window (e.g. absurdly large `Nd`) exits with a clear error rather than an uncaught traceback. |
| `-o, --output PATH` | Output zip path. Default `support-bundle.zip`. |

```bash
reyn support-bundle
reyn support-bundle --session abc123 --since 7d -o incident.zip
```

## What goes in the bundle

Three artifact classes, collected distinctly (#1833):

- **`trace/`** — the LLM payload trace, if `$REYN_LLM_TRACE_DUMP` is set and points
  at a real file.
- **`wal/`** — the WAL / crash-recovery log, `.reyn/state/**/*.jsonl` (the StateLog,
  PR21). Lives under `state/`, not `events/` — collected separately so the bundle
  stays complete.
- **`events/`** — the P6 audit logs, `.reyn/events/**/*.jsonl`.

Plus a top-level `meta.json`: reyn's own version, generation timestamp, the
`--session`/`--since` filters applied, a redacted config summary (model, configured
model classes, whether `api_base` is set — never the value), a per-file manifest
(arcname + line count), and an explicit note on how redaction was applied.

A run that finds nothing (`REYN_LLM_TRACE_DUMP` unset, no `.reyn/events/`) still
writes a valid (empty-manifest) zip and prints a note explaining why.

## Redaction

Every collected line — trace, WAL, and event log alike — is run through the
**existing** `reyn.llm.llm._redact_secrets` layer before it's written into the zip:
a parseable JSON object is redacted recursively; a non-JSON or non-object line is
wrapped and redacted the same way. Redaction is **default ON**; the only way to
disable it is `REYN_LLM_TRACE_REDACT=off` — do not share a bundle generated with
redaction off.

## Related

- [`reyn events`](events.md) — inspect/purge the same `.reyn/events/` audit logs
  this command bundles (unfiltered, unredacted — for local inspection, not sharing)
- [`reyn audit`](audit.md) — a different diagnostic surface (a static safety scan,
  not an artifact bundle)
