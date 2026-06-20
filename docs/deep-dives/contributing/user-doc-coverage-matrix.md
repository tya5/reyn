---
type: contributing
topic: docs-maintenance
audience: [agent, human]
---

# User-doc coverage matrix

A maintenance companion to [`feature-map.md`](../../feature-map.md). The
feature map answers *"what features exist"*; this matrix answers *"does every
**end-user-facing** feature have a usage how-to, and when was that last
verified"*.

It is **not** published to the public site (it lives under `deep-dives/`,
which is excluded from the mkdocs build). It is a docs-maintainer tool.

- **Last coverage sweep:** 2026-06-20
- **Last quality review:** 2026-06-20
- **Owner:** docs-maintainer (see memory pin `project_feature_map_ownership`)
- **Granularity:** the coverage matrix is one row per `feature-map.md` group
  (audience is per-group); the quality review (below) is one row per end-user
  how-to, since usage-quality is a property of each doc, not the group.

## How to read it

| Column | Meaning |
|--------|---------|
| **Group** | A `### / ####` section in `feature-map.md`. |
| **Audience** | `end-user` (uses `reyn chat` / CLI day-to-day) · `skill-author` · `reyn-developer` · `OS-internal` (no direct user surface). |
| **User how-to** | The `guide/for-users/` (or getting-started) page that teaches usage. Only required for `end-user` rows. |
| **Coverage** | ✅ covered · ⚠ partial / discoverability gap · ❌ missing · N/A (non-end-user). |
| **Verified** | Date the row was last checked against code + docs reality. |

## How to maintain it

1. **On any `feature-map.md` change** (feature added / changed / retired):
   update the matching group row here in the same PR, and bump its **Verified**
   date.
2. **A new `end-user` feature with no `guide/for-users/` how-to** is a coverage
   gap — open a how-to (EN + JA) before marking the row ✅.
3. **Periodic full sweep:** re-verify every row against current code, bump
   *Last coverage sweep* at the top. Cadence: alongside each feature-map drift
   sweep.
4. **Coverage vs quality are two passes.** *Coverage* (does a how-to exist) is
   cheap — verify file existence. *Quality* (does it teach usage well + match
   current CLI) needs a content read. The [Quality review](#quality-review)
   below is the deeper pass; re-read a how-to and bump its quality-row date
   when its feature's CLI/behaviour changes, and bump *Last quality review* on
   a full re-read.
5. **JA parity is tracked separately** — see the [JA-parity note](#ja-parity)
   below; a ✅ here means an EN how-to exists, not that JA exists.

---

## Matrix

### OS Core

| Group | Audience | User how-to | Coverage | Verified |
|-------|----------|-------------|----------|----------|
| Phase Engine | OS-internal | — (concept: architecture/principles) | N/A | 2026-06-20 |
| LLM Validation | OS-internal | — | N/A | 2026-06-20 |
| Preprocessor | skill-author | `for-skill-authors/phase-mechanics/add-a-python-preprocessor` | N/A (author) | 2026-06-20 |
| Postprocessor | skill-author | `reference/dsl/postprocessor` | N/A (author) | 2026-06-20 |
| Workspace (P5) | skill-author / OS-internal | `for-skill-authors/phase-mechanics/persist-state` | N/A (author) | 2026-06-20 |
| Crash Recovery | end-user (automatic) | for-users/index "Things Reyn handles for you" + author: `operations/crash-recovery-and-resume` | ✅ (automatic; no action needed) | 2026-06-20 |
| Time-Travel / Rewind | **end-user** | `for-users/time-travel` | ✅ | 2026-06-20 |
| Event System (P6) | reyn-developer / skill-author | `operations/debug-with-events` | N/A (author/dev) | 2026-06-20 |

### Chat Engine

| Group | Audience | User how-to | Coverage | Verified |
|-------|----------|-------------|----------|----------|
| Chat Compaction | OS-internal (automatic) | — (concept: chat-compaction) | N/A | 2026-06-20 |
| Router system prompt | OS-internal | — | N/A | 2026-06-20 |
| Plan Mode | skill-author + **end-user (`/plan` in chat)** | `for-skill-authors/composition/use-plan-mode` | ⚠ see Notes | 2026-06-20 |
| LLM router resilience | operator (config) | `reference/config/reyn-yaml` (llm block) | N/A (operator config) | 2026-06-20 |

### Platform surfaces

| Group | Audience | User how-to | Coverage | Verified |
|-------|----------|-------------|----------|----------|
| Control IR Ops | skill-author / OS-internal | `reference/runtime/control-ir` | N/A (author) | 2026-06-20 |
| Tool-Use Schemes | operator / skill-author | `concepts/tools-integrations/tool-use-schemes` | N/A (config) | 2026-06-20 |
| DSL | skill-author | `reference/dsl/*` + for-skill-authors guides | N/A (author) | 2026-06-20 |
| Stdlib Skills | end-user (auto-routed) + skill-author | for-users/index "Reyn has the skills out of the box" | ✅ (automatic routing; no per-skill how-to needed) | 2026-06-20 |
| CLI | **end-user** | getting-started + `for-users/*` (per-command, see Notes) | ✅ | 2026-06-20 |
| Config | operator | `reference/config/reyn-yaml` + user how-tos for cost/cron/auth | ✅ (user-facing knobs covered) | 2026-06-20 |
| Permissions | **end-user** | `for-users/manage-permissions` | ✅ | 2026-06-20 |
| Safety / limit-handling | end-user + skill-author | `for-skill-authors/operations/understand-why-reyn-stops` (+ `for-users/cap-spending` cross-ref) | ✅ | 2026-06-20 |
| Content-layer defense | OS-internal (security) | — (concept: security) | N/A (no user knob) | 2026-06-20 |
| Budget & Cost | **end-user** | `for-users/cap-spending` | ✅ | 2026-06-20 |
| Memory & RAG | **end-user** | `for-users/manage-memory` + `for-users/enable-semantic-search` | ✅ | 2026-06-20 |
| MCP | **end-user** | `for-skill-authors/operations/use-an-mcp-server` + `for-users/popular-mcp-servers` | ✅ | 2026-06-20 |
| Web & Protocol | end-user (web UI) + reyn-developer (A2A/REST) | `for-users/chat-and-web-ui` | ✅ (web UI; A2A/REST are developer reference) | 2026-06-20 |
| Intervention | **end-user** | `for-users/ask-user-mid-phase` | ✅ | 2026-06-20 |
| Sessions and identity | OS-internal / concept | `concepts/multi-agent/sessions` | N/A | 2026-06-20 |
| Multi-Agent | skill-author | `for-skill-authors/composition/*` | N/A (author) | 2026-06-20 |
| TUI | **end-user** | `for-users/chat-and-web-ui` | ✅ (chat UI; panel details largely self-evident) | 2026-06-20 |
| Sandbox | end-user / operator | `for-users/configure-sandbox` | ✅ | 2026-06-20 |
| Environment | operator (experimental) | — (⚗ Stage 2 MVP) | N/A (experimental) | 2026-06-20 |

### Auth & scheduling (CLI sub-surfaces)

These are end-user CLI capabilities surfaced under the CLI / Config groups but
tracked here explicitly because each needed its own how-to:

| Capability | User how-to | Coverage | Verified |
|-----------|-------------|----------|----------|
| OAuth login (`reyn auth`) | `for-users/oauth-login` | ✅ | 2026-06-20 |
| Cron scheduling (`reyn cron`) | `for-users/schedule-skills` | ✅ | 2026-06-20 |

---

## Quality review

One row per end-user how-to. **Quality** = does it teach usage (steps +
examples + troubleshooting) and match current CLI/behaviour. **Drift** = how
the CLI/impl alignment was checked.

| How-to | Quality | Drift check | Verified |
|--------|---------|-------------|----------|
| `for-users/cap-spending` | ✅ strong | impl-verified (slash cmds / cost config / ledger) | 2026-06-20 |
| `for-users/schedule-skills` | ✅ strong | impl-verified (cron CLI + CronJobConfig) | 2026-06-20 |
| `for-users/oauth-login` | ✅ strong | impl-verified (auth CLI + provider fields) | 2026-06-20 |
| `for-users/manage-memory` | ✅ strong | impl-verified (memory CLI subcommands) | 2026-06-20 |
| `for-users/time-travel` | ✅ strong | content read; no drift seen | 2026-06-20 |
| `for-users/manage-permissions` | ✅ strong | content read; no drift seen | 2026-06-20 |
| `for-users/chat-and-web-ui` | ✅ strong | content read; shortcuts/commands current | 2026-06-20 |
| `for-users/enable-semantic-search` | ✅ strong | content read; `reyn embeddings` current | 2026-06-20 |
| `for-users/work-with-files` | ✅ strong | content read; no drift seen | 2026-06-20 |
| `for-users/configure-sandbox` | ✅ strong | content read; per-backend tables current | 2026-06-20 |
| `for-users/popular-mcp-servers` | ✅ strong | content read (per-server catalog) | 2026-06-20 |
| `for-skill-authors/operations/use-an-mcp-server` | ⚠ note | content read; **stale TODO comment** (see Open items) | 2026-06-20 |
| `for-skill-authors/composition/use-plan-mode` | ✅ strong content / ⚠ discoverability | content read; `/plan` cmds current | 2026-06-20 |
| `for-skill-authors/operations/understand-why-reyn-stops` | ✅ strong | content read; reflects #1877 `ask_on_exceed` removal | 2026-06-20 |
| `for-users/ask-user-mid-phase` | ⚠ scope | content read (see Open items) | 2026-06-20 |

**Headline:** every end-user how-to is a genuine usage doc (steps + examples +
troubleshooting), not a stub. No CLI drift found in the 2026-06-20 read. Two
minor quality notes + one discoverability note are tracked below.

---

## Open items

### `use-an-mcp-server` stale TODO comment (⚠ quality)

`docs/guide/for-skill-authors/operations/use-an-mcp-server.md` carries an
inline authoring TODO: `<!-- TODO: confirm extra name ([mcp]) once PR32 lands
… -->`. The `[mcp]` extra is in fact confirmed correct (CI installs
`.[dev,mcp,web]`), so the TODO is stale and should be dropped. Low-risk
one-line cleanup.

### `ask-user-mid-phase` audience/scope mismatch (⚠ quality)

The for-users hub links this as *"Respond mid-task — answer questions Reyn
asks."* But the doc's content teaches a **skill author** how to emit an
`ask_user` op from a phase — author-facing material under a user-facing
framing. The genuine end-user action (typing an answer at a prompt) is trivial
and may not need a doc. **Candidate:** re-home this under for-skill-authors, or
re-scope a short user-facing "answering prompts" note. Flagged for owner/lead.

### Plan Mode end-user discoverability (⚠)

### Plan Mode end-user discoverability (⚠)

`/plan` is invokable by end users inside `reyn chat`, but the only how-to
(`use-plan-mode`) lives under **for-skill-authors** and is framed for authors.
An end user browsing `for-users/` will not find plan-mode usage. **Candidate:**
either a short `for-users` plan-mode how-to, or a cross-link from the user hub.
Not yet actioned — flagged for owner/lead prioritization.

### JA parity

A ✅ in this matrix means an **EN** how-to exists. Japanese parity is a separate
axis tracked outside this matrix. As of the last full sweep, several
`for-users/` pages still lacked `.ja.md` (e.g. chat-and-web-ui,
enable-semantic-search, time-travel, work-with-files, popular-mcp-servers). New
how-tos in the 2026-06-20 wave shipped EN+JA together; the backlog of
pre-existing EN-only user pages is the remaining JA gap.

---

## Result of the 2026-06-20 sweep

**Coverage:** every `end-user` feature group has an EN usage how-to (✅). All
non-end-user groups are correctly served by concept / reference / author docs
(N/A). The earlier gaps (budget / cron / auth / memory how-tos, and time-travel
/ semantic-search discoverability) were closed in the 2026-06-20 docs wave.

**Quality:** every end-user how-to was read and is a genuine usage doc (steps +
examples + troubleshooting), not a stub; no CLI drift found. Three follow-ups,
all minor and tracked in Open items: plan-mode discoverability (⚠),
`use-an-mcp-server` stale TODO (⚠), `ask-user-mid-phase` audience mismatch (⚠).

**Net:** the answer to "is every end-user feature documented for users?" is
**yes for coverage, yes for quality**, with three minor follow-ups (none a
missing-doc gap) and JA parity tracked as a separate axis.
