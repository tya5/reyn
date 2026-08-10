---
type: reference
topic: runtime
audience: [human, agent]
---

# Tool naming convention

This page is the canonical record of the naming convention for tool names —
**the one name each tool has**, e.g. `read_file`. It exists so a reviewer can
check "does this new tool name fit the convention" without re-deriving the rule
from a census each time, and so `tests/tools/test_tool_naming_convention_gate_3223.py`
(the drift-prevention CI gate) has a human-readable rationale to point at.

> **#3429 — there is no second namespace.** Until 2026-07-29 every catalog
> action also had a `<category>__<verb>` **qualified** name (`file__read` for
> `read_file`), and this page documented both. Two names for one operation meant
> every subsystem that keys on a tool name had to decide whether to handle both;
> a census of the 11 that exist found 4 with explicit two-form compensation and 7
> without. The qualified namespace is abolished. A category survives only as the
> browsing axis `list_actions(category=[…])` exposes.
>
> `tests/tools/test_no_qualified_tool_names_3429.py` keeps it abolished: it walks the
> live registry, the catalog's membership table, the categories tuple, and the
> assembled `tools=` payload, and fails on any `__` in a name.

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
- **There is no second, category-prefixed form.** The `<category>__<verb>`
  addressing design was a different namespace with its own internally-consistent
  scheme, and #3429 removed it. A `__` in a tool name is now a convention
  violation the gate rejects, not a different namespace.
- **Family-prefix groups are grandfathered as a whole**: `cron_*`,
  `pipeline_*`, `skill_*`, `mcp_*` (the legacy `mcp_verbs.py` object_verb
  group), `reyn_repo_*`, and `web_*` all use `<object>_<verb>` order
  internally. **Family-internal consistency beats the global rule** — when
  adding a new member to one of these families, follow that family's
  existing order, not R1's flat default. Do not "fix" a family member to
  verb_object in isolation; that breaks family-internal consistency instead
  of upholding it.

### Family-prefix validity condition (#4004, owner-ratified)

A family-prefix exemption is valid **only when it holds the object's tools
completely** — every tool for that object uses the family's `object_verb`
order, with no `verb_object`-ordered sibling naming the same object
elsewhere in the registry. When a `verb_object` tool for the same object
DOES exist, the family does not hold: R1's flat default governs, and the
`object_verb` member(s) are the ones out of step, not the exemption.

This was not a hypothetical concern: a live registry census found the
decisive counter-example — `pipeline_list` (`object_verb`) and
`list_agents` (`verb_object`) name the SAME operation class (`list`) in
opposite word orders, with no semantic distinction between them (a
resource-generation-vs-listing hypothesis does not hold — `pipeline_list`
itself is proof against it). The census also found `agent_spawn` /
`session_spawn` (`object_verb`, informally called a "spawn family" though
never documented as one above) coexisting with no `verb_object` sibling for
the same object at the time — but `topology_create`'s later rename (#4004)
to `create_topology` illustrates the direction this condition points:
grandfathering an `object_verb` anomaly is only a name for "not yet fixed",
not a permanent exemption, once a working `verb_object` alternative exists
for the same object.

This section records the DIRECTION, not a retroactive ruling on every
existing family: `pipeline_*` and `skill_*` remain grandfathered above
pending their own future reconciliation (out of scope for #4004, which
resolved only the `agent_spawn`/`session_spawn`/`topology_create` case) —
this condition governs how a FUTURE addition to any family should be
judged, and flags which existing families are candidates for the same
treatment.

## R2 — removal: four verbs, four *distinct* classes (frozen)

Census inspection found that the apparent "3-verb inconsistency" in removal
naming is not inconsistency — it is four *different* operation classes that
each already picked one verb, and the verb choices carry real distinctions
(reversibility, and symmetry with another verb):

| verb | operation-class (meaning) | example tools |
|---|---|---|
| `delete` | irreversible filesystem delete | `delete_file` |
| `drop` | deregister from an index (entity may still exist elsewhere) | `mcp_drop_server` (FP-0066 P1b retired the RAG `drop_source`) |
| `forget` | memory removal — the dual of `remember` | `forget_memory` |
| `uninstall` | the dual of `install` | `uninstall_plugin` |

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
  `read_memory_body`).
- **`describe`** = fetch **metadata** (e.g. `describe_agent`,
  `describe_action`). This is a distinct
  operation-class from `read`, not a competing synonym for it — introspection
  and content retrieval are different things.

`get_mcp_prompt`'s `get` is the sole existing exception (content-fetch that
predates this convention) — grandfathered by name. **New tools must not use
`get`**; pick `read` or `describe` per the class above.

`remove` / `fetch` / `retrieve` are 0-count in the current census and are not
part of the canonical verb set for new names (though `fetch` remains valid
as the verb of the pre-existing `web_fetch`, itself part of the grandfathered
`web_*` family).

## load class (distinct from read — FP-0066)

`load` is fetch **+ activation** — the fetched content is not just returned,
it becomes active in context (e.g. a skill's instructions get attached to
the run). This is why `load` is a distinct operation-class from `read`:
calling a `read_*`-named tool would misleadingly suggest the result is inert
content, when it actually changes what's active.

`load_skill` is canonical per the FP-0066 ruling — `load` is added to the
canonical verb lexicon for this reason.

## R4 — install: source-split is canonical

New install tools use the source-split pattern: `install_local` /
`install_source` / `install_package` / `install_registry`. The name states
the source explicitly, so the LLM doesn't have to infer/guess which source
kind applies — a install call errs toward being unambiguous about capability
rather than making the LLM disambiguate via a discriminated argument.

**Grandfathered**: single discriminated-arg install tools that predate this
split — the `install_plugin` / `uninstall_plugin` / `list_plugins` trio,
`presentation_install_local`, and the orphaned `mcp_install` (superseded by the
`mcp_install_local`/`_package`/`_registry` split on 2026-05-25; not a catalog
action any more — flagged as dead surface, candidate for a separate retire
follow-up).

The plugin trio takes R1's `verb_object` default rather than the `object_verb`
order its sibling install families use, and #3429 records why: they were
`plugin_management__install` / `__uninstall` / `__list` — the only registry
entries that ever carried the catalog separator inside a flat name — so there
was no pre-existing flat `plugin_*` tool family for them to be internally
consistent WITH. `plugin_install` / `plugin_uninstall` were also unavailable:
those are OP KINDS, and op kinds share one canonical-declaration namespace with
tool names, so reusing them raises a conflicting-declaration error at import.

## R5 — retired with the namespace it governed (#3429)

R5 said a catalog category with exactly one member must not repeat the category
name as its own verb (`exec__exec`, `skill_management__load_skill`), and
prescribed a clean action-verb instead. It was a rule about how to compose the
`<category>__<verb>` form, so it went with that form.

Two of its rulings survive as ordinary R1 names and are recorded here because
the verbs they minted are still in the lexicon:

- **`exec`** — `run` was chosen as the canonical verb for the "execute an
  ephemeral subprocess" class (`spawn` is reserved for long-lived entities:
  `spawn_agent`, `spawn_session` — renamed from `agent_spawn`/`session_spawn`,
  #4004). The tool itself is `exec`.
  ★ Note for CodeAct: `exec` collides with a BANNED builtin, so
  `encoders.sanitize_identifier` suffixes it (`exec_`) when rendering the
  code-API. That is a rendering concern, not a second name — the gate still
  receives `exec`.
- **`load_skill`** — `load` is fetch + activation, a distinct operation-class
  from `read` (see the load-class section above).

A tool name that contains `__` is now a violation of R1, caught by
`tests/tools/test_no_qualified_tool_names_3429.py`.

## Grandfathered anomalies with no family (individual, pre-existing)

A handful of pre-existing names fit neither R1's verb_object rule nor an
established family-prefix group. Each is grandfathered individually rather
than invented into a new "rule":

- `hooks_add` — sole `hooks_*`-shaped tool, predates the convention.

> #4004 (owner-ratified): `topology_create` used to be grandfathered here
> ("sole tool of its shape, no established family") — renamed to
> `create_topology`, now a compliant `verb_object` name (see "Canonical verb
> lexicon" below for the newly added `create` verb), no longer an anomaly.

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
verbs" above), `create` (#4004 — added for `create_topology`, renamed from
`topology_create`).

This lexicon (and the grandfather frozen-set) is reconciled against the full
live registry census in `tests/tools/test_tool_naming_convention_gate_3223.py` —
that test file is the executable source of truth for which names are
currently registered; this doc is the human-readable rationale for the
rules the test enforces, plus the semantic rules (R2/R3 class-selection,
load vs read) the test explicitly does not and cannot enforce.
