---
type: reference
topic: runtime
audience: [human, agent]
---

# Tool naming convention

This page is the canonical record of the naming convention for tool names
(the flat registry names an agent calls, e.g. `read_file`, and the catalog
`category__verb` qualified names, e.g. `file__read`). It exists so a reviewer
can check "does this new tool name fit the convention" without re-deriving
the rule from a census each time, and so `tests/test_tool_naming_convention_gate_3223.py`
(the drift-prevention CI gate) has a human-readable rationale to point at.

**Owner-ratified policy (#3223): no rename sweep.** Every name that predates
this convention (or intentionally sits outside it) is grandfathered by name in
the gate's frozen allowlist, with a reason. The convention only constrains
**new** tool names going forward.

## STRUCTURAL vs SEMANTIC — what the gate can and cannot enforce

Naming has two different kinds of rules, and only one kind is gate-able:

- **STRUCTURAL** (§ R1, R5 word-order/shape; the closed removal-verb set and
  the closed fetch-one-verb set in R2/R3 *as sets*): a syntactic property of
  the name string itself. An AST/registry enumeration can check these with
  zero false positives, because the check never has to guess what a name
  *means* — only what shape it has.
- **SEMANTIC** (§ R2/R3/load-class: *which* operation-class a new tool
  belongs to — e.g. "should this new tool use `delete` or `drop`?"): this is
  an intent-reading judgment about what the tool actually does. No syntactic
  gate can check "did the author pick the right class" without effectively
  re-implementing code review. **The gate below does not attempt this** — it
  is enforced by doc + human/reviewer judgment against the tables in this
  file, not by CI.

Do not read a green gate run as "the tool picked the semantically correct
verb" — it only means "the verb used belongs to the closed canonical set, in
the right position, and isn't a 5th removal/fetch verb."

## R1 — word order

- **New flat names**: `<verb>_<object>` (e.g. `read_file`, `list_agents`,
  `search_actions`). This is the plurality pattern among current flat names.
- **Catalog qualified names**: `<category>__<verb>` (e.g. `file__read`,
  `mcp__list_servers`). This is the catalog's addressing *design* — object
  (category) first, then verb — and is not in conflict with R1's flat-name
  rule; it is a different namespace with its own internally-consistent
  scheme.
- **Family-prefix groups are grandfathered as a whole**: `cron_*`,
  `pipeline_*`, `skill_*`, `mcp_*` (the legacy `mcp_verbs.py` object_verb
  group), `reyn_repo_*`, and `web_*` all use `<object>_<verb>` order
  internally. **Family-internal consistency beats the global rule** — when
  adding a new member to one of these families, follow that family's
  existing order, not R1's flat default. Do not "fix" a family member to
  verb_object in isolation; that breaks family-internal consistency instead
  of upholding it.

## R2 — removal: four verbs, four *distinct* classes (frozen)

Census inspection found that the apparent "3-verb inconsistency" in removal
naming is not inconsistency — it is four *different* operation classes that
each already picked one verb, and the verb choices carry real distinctions
(reversibility, and symmetry with another verb):

| verb | operation-class (meaning) | example tools |
|---|---|---|
| `delete` | irreversible filesystem delete | `delete_file`, `file__delete` |
| `drop` | deregister from an index (entity may still exist elsewhere) | `mcp_drop_server`, `mcp__drop_server` (FP-0066 P1b retired `drop_source` / `rag_operation__drop_source`) |
| `forget` | memory removal — the dual of `remember` | `forget_memory`, `memory_operation__forget` |
| `uninstall` | the dual of `install` | `plugin_management__uninstall` |

**Why NOT unify to a single `remove` verb**: doing so would erase two things
that carry real meaning:
1. The **safety distinction** between `delete` (destructive, cannot be
   undone) and `drop` (deregistration — the underlying entity typically
   survives). Collapsing both into `remove` hides which one is dangerous.
2. The **dual-verb symmetry**: `remember` ↔ `forget` and `install` ↔
   `uninstall` are meaningful pairs an LLM can reason about ("this undoes
   that"). A flat `remove` breaks both pairings and reduces legibility.

**This table is frozen as canonical.** A **new** removal verb (a 5th verb
beyond `delete`/`drop`/`forget`/`uninstall`) is a convention violation the
gate rejects — pick the class from this table instead of inventing a new
verb. `verb → class` is injective (each verb maps to exactly one class);
there is deliberately no "catch-all" removal verb.

### Dual-pair verbs — the general pattern R2 is one instance of

Several canonical verbs exist specifically as the *inverse* of another
canonical verb — an intentional symmetry an LLM can reason about ("this
undoes that"), not an accident of naming:

| pair | meaning | class |
|---|---|---|
| `install` ↔ `uninstall` | register a capability ↔ its removal | R4 install / R2 removal (uninstall row) |
| `remember` ↔ `forget` | write memory ↔ its removal | memory write / R2 removal (forget row) |
| `register` ↔ `unregister` | activate a scheduled/registered entity ↔ deactivate it | lifecycle registration (not a removal — the entity is not deleted, dropped, forgotten, or uninstalled, only deregistered from the scheduler) |
| `enable` ↔ `disable` | turn a registered entity on ↔ off | lifecycle toggle (not a removal — no deregistration happens, only a status flip) |

`register`/`unregister`/`enable`/`disable` are in the canonical verb lexicon
(so a new tool may use them, either in `verb_object` position per R1's flat
default, e.g. `register_webhook`, or in the pre-existing `cron_*` family's
`object_verb` order, e.g. `cron_register` / `cron_unregister` /
`cron_enable` / `cron_disable`) precisely so a future cron-like family is
not false-rejected for legitimately reusing this lifecycle-toggle dual
pattern.

**What is gated vs what is not**: the gate checks that a name's verb token
belongs to the canonical lexicon, is positioned correctly for its
namespace/family, and — for the R2 removal table specifically — is one of
the frozen four rather than a 5th removal verb. **Whether a dual pair's
*counterpart* actually exists (e.g. "if there's a `register_x`, is there
really an `unregister_x`?") is a SEMANTIC check, not a structural one** —
verifying real symmetry requires reading what the paired tool does, which
is intent-reading, the same reason R2/R3's class-selection is doc+review
only (see the STRUCTURAL vs SEMANTIC section above). The gate does not and
cannot assert "this dual pair is complete."

(`cron_unregister` is a family-scoped exception: the `cron_*` family already
grandfathered under R1 uses `register`/`unregister` as its own dual pair,
scoped to that family only — it does not add a 5th verb to the general
removal-class table above.)

## R3 — fetch-one: two classes

- **`read`** = fetch **content** (e.g. `read_file`, `read_mcp_resource`,
  `memory_operation__read`).
- **`describe`** = fetch **metadata** (e.g. `describe_agent`,
  `describe_action`, `multi_agent__describe_peer`). This is a distinct
  operation-class from `read`, not a competing synonym for it — introspection
  and content retrieval are different things.

`get_mcp_prompt`'s `get` is the sole existing exception (content-fetch that
predates this convention) — grandfathered by name. **New tools must not use
`get`**; pick `read` or `describe` per the class above.

`remove` / `fetch` / `retrieve` are 0-count in the current census and are not
part of the canonical verb set for new names (though `fetch` remains valid
as the qualified-name verb for the pre-existing `web__fetch`, itself part of
the grandfathered `web_*` family).

## load class (distinct from read — FP-0066)

`load` is fetch **+ activation** — the fetched content is not just returned,
it becomes active in context (e.g. a skill's instructions get attached to
the run). This is why `load` is a distinct operation-class from `read`:
calling a `read_*`-named tool would misleadingly suggest the result is inert
content, when it actually changes what's active.

`load_skill` (flat) / `skill_management__load` (qualified) are canonical
per the FP-0066 ruling — `load` is added to the canonical verb lexicon for
this reason. (`skill_management__load_skill` would be rejected under R5 —
see below — since it repeats the category word in the verb position.)

## R4 — install: source-split is canonical

New install tools use the source-split pattern: `install_local` /
`install_source` / `install_package` / `install_registry`. The name states
the source explicitly, so the LLM doesn't have to infer/guess which source
kind applies — a install call errs toward being unambiguous about capability
rather than making the LLM disambiguate via a discriminated argument.

**Grandfathered**: single discriminated-arg install tools that predate this
split — `plugin_management__install` (and the legacy flat
`plugin_management__install`/`plugin_management__uninstall`/
`plugin_management__list` trio, which additionally uses catalog-style
`__` inside a *flat* registry name — grandfathered as a pre-existing group),
`presentation_management__install`, and the orphaned `mcp_install` (superseded
by the `mcp_install_local`/`_package`/`_registry` split on 2026-05-25; not
present in `_OPERATION_RULES` any more — flagged as dead surface, candidate
for a separate retire follow-up, out of scope for this PR).

## R5 — single-entry categories never form `X__X`

A catalog category with exactly one member must not repeat the category name
as its own verb (`exec__exec`, `skill_management__load_skill`). Instead use
a clean canonical action-verb for that operation-class:

- `exec__run` (not `exec__exec`) — `exec` is the only single-entry category
  besides `presentation_management`; `run` is the canonical verb for the
  "execute an ephemeral subprocess" class (`spawn` is reserved for
  long-lived entities: `agent_spawn`, `session_spawn`).
- `knowledge__search` (FP-0066 P3c, #3247 firm §3) — another single-entry
  category (semantic search over the operator's own skill/memory/repo
  knowledge, flat name `search_knowledge`). `search` was already in the
  canonical verb lexicon (`search_actions` / `mcp_search_registry`), so this
  fits R1's `verb_object` pattern with no lexicon addition and no
  grandfather entry needed — unlike `exec`/`load`, `knowledge` did not need
  a new verb minted for it.
- `skill_management__load` (not `skill_management__load_skill`).
- `presentation_management__install` is **not** an X__X violation — the
  category is `presentation_management`, the verb is `install`; they are not
  equal, so no rename is needed there (verb ≠ category word by construction).

Catalog verb differing from the registry (flat) tool name is an existing,
accepted pattern, not a new mechanism: `file__delete` ↔ registry
`delete_file`, `multi_agent__describe_peer` ↔ registry `describe_agent`,
`reyn_repo__read` ↔ registry `reyn_repo_read`.

## Grandfathered anomalies with no family (individual, pre-existing)

A handful of pre-existing names fit neither R1's verb_object rule nor an
established family-prefix group. Each is grandfathered individually rather
than invented into a new "rule":

- `hooks_add` — sole `hooks_*`-shaped tool, predates the convention.
- `topology_create` — sole tool of its shape, no established family.

> FP-0066 P1b retired the agent-facing layer-1 in-core RAG tools
> (`semantic_search`, `index_update`, `drop_source`, `list_rag_sources`)
> along with the `rag_operation` category — the `index_update` /
> `semantic_search` naming-anomaly grandfather entries that used to live
> here no longer apply. See
> [proposal 0066 §9](../../deep-dives/proposals/0066-retrieval-two-groups-two-axes.md).

## Canonical verb lexicon (as reconciled against the live registry)

`list`, `read`, `describe`, `delete`, `drop`, `forget`, `uninstall`,
`install_local`, `install_source`, `install_package`, `install_registry`,
`run`, `load`, `search`, `fetch`, `spawn`, `call`, `remember`, `render`,
`present`, `compact`, `ask`, `edit`, `emit`, `glob`, `grep`, `invoke`,
`subscribe`, `unsubscribe`, `write`, `delegate`, `embed`, `exec`,
`register`, `unregister`, `enable`, `disable` (the last four accepted in
either `verb_object` position, per R1's flat default, or `object_verb`
suffix position for the pre-existing `cron_*` family and any future
cron-like family reusing this lifecycle-toggle dual pattern — see "Dual-pair
verbs" above).

This lexicon (and the grandfather frozen-set) is reconciled against the full
live registry census in `tests/test_tool_naming_convention_gate_3223.py` —
that test file is the executable source of truth for which names are
currently registered; this doc is the human-readable rationale for the
rules the test enforces, plus the semantic rules (R2/R3 class-selection,
load vs read) the test explicitly does not and cannot enforce.
