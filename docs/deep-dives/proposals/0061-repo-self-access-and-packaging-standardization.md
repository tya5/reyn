---
status: draft (2026-07-13)
supersedes: 0060 §D5b (the build-time docs mirror mechanism)
preserves: 0060 F1 production-reachability invariant (upgraded, not removed)
---

# 0061 — Repo self-access + packaging standardization

**One line:** make reyn's own repo (README + CHANGELOG + all of `docs/` + source)
reachable from inside a *pure wheel* install through a single deterministic
toolset, by replacing the bespoke build-time docs mirror with the standard
build-backend mechanism (Hatchling `force-include`) — and rename the
now-misnamed `reyn_src` toolset to `reyn_repo`.

> **Layer split (why this is 0061, not a 0060 addendum).** 0060 is *LLM
> wielding — why the OS ships builtins/docs at all*. 0061 is *docs-delivery
> infrastructure — how those files physically reach a production install*. The
> build-backend migration's blast radius (release / CI / packaging) exceeds
> 0060's capability thesis, so it lives in its own proposal. 0061 **supersedes
> 0060 §D5b** (the mirror) and **preserves + upgrades** the F1 reachability
> invariant; 0060 §D5b gets a superseded-pointer to here.

---

## 1. Problem — three coupled defects

1. **Reinvention (dormant).** `read_builtin_doc` (0060 #2911) was built to read
   bundled reference docs in a wheel. But the `reyn_src` toolset already exists
   for exactly "read reyn's own source + docs from inside," and its own
   docstring (L22-27) *pre-announces the wheel fallback as a follow-up*.
   `read_builtin_doc` has **zero production callers** (only test + smoke) — it
   is a parallel dormant mechanism for a job `reyn_src` was designed to own.

2. **Wheel-unreachable.** `reyn_src_*` resolves through `resolve_reyn_root()`,
   which walks up from `reyn.__file__` for a `name = "reyn"` pyproject and
   **raises `RuntimeError` in a pure wheel install** (no co-located repo). Its
   error message literally says *"needs a development install … wheel-install
   support is tracked as a packaging follow-up."* So README / `docs/` / source
   are unreachable via `reyn_src` in production `pip install reyn`.

3. **Hacky bundling.** `docs/reference/` is shipped into the wheel by a custom
   `build_py` cmdclass that `shutil.copytree`s it into a **git-ignored**
   `src/reyn/builtin/reference/` mirror, guarded by a byte-identity CI test.
   This is a workaround for a setuptools limitation ("package data must live
   *inside* the package dir"), not the standard approach.

**Owner expectation (the target):** `README.md`, `CHANGELOG.md`, and **all of
`docs/`** reachable via the repo toolset in **both dev and pure-wheel**, the
mkdocs site still builds, and **`semantic_search` stays opt-in** (the floor is
deterministic, embedding-free `reyn_repo` reads — retrieval is never the
primary discovery surface).

## 2. The standard model (grounded)

PyPA / setuptools: bundled data must end up **inside a package directory** and
be read via **`importlib.resources`**. "Data files outside the package
directory are no longer allowed" in setuptools — hence the copytree mirror. The
*fundamental* half (data must land in a package for `importlib.resources` to
reach it in a wheel) is backend-independent; the *"source files must physically
sit inside the package dir, else a build hook copies them"* friction is
**setuptools-specific**.

**Hatchling `force-include`** removes the friction declaratively:

```toml
[tool.hatch.build.targets.wheel.force-include]
"README.md"    = "reyn/_bundled/README.md"
"CHANGELOG.md" = "reyn/_bundled/CHANGELOG.md"
"docs"         = "reyn/_bundled/docs"
```

The sources stay at repo root (mkdocs `docs_dir: ../docs` unaffected); at
*wheel-build time* they are mapped into the installed package tree; a normal
data read (`importlib.resources` / filesystem path) reaches them. No cmdclass,
no copytree script, no git-ignored generated tree, no byte-gate test.
`docs/` is ~11 MB / 745 md files → a few MB compressed into the wheel — the
accepted cost of full self-documentation reachability.

## 3. Design

### 3.1 Build backend: setuptools → Hatchling

Enumerate → map → gate omissions (the omission risk is a silently-broken
wheel). Grounded config surface:

| setuptools (today) | Hatchling (target) | note |
|---|---|---|
| `[build-system] setuptools, wheel` | `hatchling` | |
| `[tool.setuptools.packages.find] include=["reyn*"]` | `[tool.hatch.build.targets.wheel] packages=["src/reyn"]` | |
| `[tool.setuptools.package-data] "reyn"=["py.typed","builtin/**/*","environment/*.Dockerfile"]` | Hatchling include rules | **omission risk — drop any of the 3 → broken wheel** |
| `setup.py` `build_py` cmdclass (mirror) | **deleted** | replaced by `force-include` |
| `[project.scripts]`, `[project.entry-points."reyn.webhooks"]` | unchanged | PEP 621 standard = backend-independent |
| static `version` | unchanged | simple |

A CI check asserts the built wheel contains `py.typed`, `builtin/**`, the
Dockerfiles, and the `_bundled/` tree (§3.4 parity gate covers the last).

### 3.2 `reyn_repo` resolver — dual-mode, one logical namespace

`resolve_reyn_root()` gains a **wheel mode**, detected by the **presence of the
`_bundled/` dir adjacent to `reyn.__file__`** (NOT "walk-up failed" — a failure
fallback could resolve a weird checkout to an unrelated `name=reyn`-ish tree =
confused-deputy scoping). Detection order:

1. `<pkg>/_bundled/` exists → **wheel mode**, roots = installed package.
2. else walk up for `name="reyn"` pyproject → **dev mode**, root = repo root.

Callers (`read` / `list` / `grep` / `glob`) are **unchanged** — they already
take a `root: Path` and resolve within it via `safe_resolve_inside`. Only the
resolver changes. A thin **logical↔physical normalization** presents one
repo-relative namespace in both modes:

| logical path (what the LLM uses) | dev physical | wheel physical |
|---|---|---|
| `README.md` / `CHANGELOG.md` | `<repo>/README.md` | `<pkg>/_bundled/README.md` |
| `docs/x.md` | `<repo>/docs/x.md` | `<pkg>/_bundled/docs/x.md` |
| `src/reyn/foo.py` | `<repo>/src/reyn/foo.py` | `<pkg>/foo.py` |

### 3.3 Single reachable-set SSoT

The set of reachable roots — `{README.md, CHANGELOG.md, docs/, src/reyn/}` — is
**declared once** and drives **both**:

- the wheel **`force-include`** map (what ships), and
- the dev-mode **allowlist** (what `reyn_repo` will resolve).

Deriving the dev allowlist from the same declaration (no hand-copy) is what
makes dev and wheel expose *exactly the same set by construction*: dev does not
over-expose `tests/` / `.git/`; wheel does not under-expose. This obeys
`preflight-gate-must-derive-path-from-ssot`.

### 3.4 dev == wheel parity (the anti-confusion invariant + gate)

**Invariant:** for every logical path in the declared set, a dev install and a
pure-wheel install resolve it to the **same logical path** and **byte-identical
content**; and any path *outside* the declared set is **refused in both**.

**CI parity gate** (evolves #2920 from "reference reachable" → "dev/wheel
namespace parity"):

- POSITIVE: build a wheel, install into a clean venv, assert `reyn_repo` reads
  README + a `docs/` file + a source file, byte-identical to the dev checkout.
- **NEGATIVE (flip-witness, required):** a non-declared path (e.g. `tests/foo`)
  is **refused** in dev — equivalent to "absent in wheel". Without the negative
  case a positive-set-only test mistakes *coverage* for *parity*
  (`bound-test-must-flip-under-strip`). Strip the SSoT-derivation → the
  over-exposure (dev reads `tests/`) must make the gate RED.

### 3.5 Retire / keep (scoped — do not disturb working paths)

**Retire** (all serve the dormant `read_builtin_doc` path or the mirror hack):
`read_builtin_doc`, `scripts/mirror_reference_docs.py`, the `build_py`
cmdclass, the byte-gate test (`tests/test_0060_d5b_docs_mirror.py`), the
git-ignored `src/reyn/builtin/reference/` mirror.

**Keep untouched:** `read_builtin_body_bytes` — it is **production-LIVE**
(`file.py:214`, the `read` op reading builtin skill/pipeline bodies), already
wheel-correct (in-package `builtin/`, #2920-gated), and serves a *different*
purpose than doc-reading. Pulling a working path into a dormant-path fix
expands blast radius onto a working surface (`recall-original-purpose`).
Unifying it with `reyn_repo` is **out of scope** here; it may be revisited only
if a future parity gate covers *both* namespaces (bundled docs + in-package
bodies).

### 3.6 Rename `reyn_src` → `reyn_repo`

The toolset now reads the whole repo (README + docs + source), so `_src` is a
misnomer. Clean-break rename (repoint + delete, **no alias** — owner
clean-end-state). Three grounded points:

**A. Unify BOTH naming layers (the name is already split today).** There are
two layers that have drifted apart, and the rename must target both or it
reproduces the drift:
- **category** = `reyn_source` (`universal_catalog.py:80,569`, SP frame in
  `universal_slots.py`)
- **tool-stem** = `reyn_src_*` (`reyn_src.py` ToolDef names
  `reyn_src_{read,list,glob,grep}`)
- **dispatch key** = `reyn_source__read → reyn_src_read`
  (`universal_dispatch.py:281-284`)

→ Unify to **`reyn_repo` across category + tool-stem**, and **retire the legacy
`reyn_source` category** in the same clean-break (do not carry the src/source
inconsistency forward).

**B. Derive the rename surface from the registration SSoT, not name-grep
alone** (`clean-break-completeness-full-repo-grep` +
`verify-existence-against-registry-not-namegrep`). Grounded live surfaces:
ToolDef names (`reyn_src.py`), tool descriptions (`descriptions/dev.py`),
**cross-references inside other tools' descriptions**
(`discovery.py:65,110` "prefer over `reyn_src_read`", `memory.py:351`), SP
frame (`universal_slots.py`), category list (`universal_catalog.py`), dispatch
map (`universal_dispatch.py`), and the **cold-start LIVE seed**
(`action_usage_tracker.py:117` `reyn_source__list`). Full-repo grep minus the
historical allowlist.

**C. Live-vs-historical carve-out (grounded, owner-confirmed).** Two real
categories in the same file: `action_usage_tracker.py:117` is a **LIVE seed**
(rename target), while `:84` is a **B27-M5 historical comment** (a record of
what happened — immutable). The `ADR-0026` reference (`reyn_src.py:1`) is
likewise a historical record. Rename only live surfaces; the historical
`docs/deep-dives/journal/` entries and these in-code historical records are
**not rewritten** (`module-rename-drift-distinguish-public-surface`).

Done in the same arc as the resolver change (the modules are already touched),
one coherent rename, not a second pass over the same files.

*De-risk (grounded):* the 0060 builtin cheat-sheet does **not** hardcode
`reyn_src_*` tool names (only `builtin/docs.py` references them, and that is a
retire-target) — so the rename has **no** cross-arc breakage against
0060-landed content; it is decoupled from 0060.

## 4. Disposition of 0060 (exhaustive — no loose ends)

0061 supersedes one 0060 mechanism (§D5b) and, in resolving this discussion,
settles several 0060 open items. This section accounts for **every** 0060
component so nothing is left dangling. The 0061 arc includes an edit to 0060
itself applying every "→" below (status line, fork resolutions, §D5b
superseded-pointer, D10 declined-note, held-item marks).

**Floor (F) — all LANDED, no action:**

| item | disposition |
|---|---|
| F1 catalog + part-type meta-registry + taxonomy gate | ✅ landed (#2899 + catalog) |
| F2 SP routing model (part×role map) | ✅ landed (router_frame) |
| F3 builtin exemplar set + through-chain (builtin tier) | ✅ landed — curated-5 **4/5** (cheat-sheet + flagship pipeline + draft_judge_revise + status_card); #5 hook exemplar = Fork-1 defer (inline in cheat-sheet, owner-overridable) |
| F4 `presentation_management__install_*` | ✅ landed (Layer A) |
| Layers A/B/C backbone (provenance / SSoT / routing frame) | ✅ landed |

**Enhancement (E):**

| item | disposition |
|---|---|
| E1 `search_actions` auto-gate | ✅ pre-existing, unchanged |
| E2 authoring corpus — *packaging half* ("are docs in the wheel?") | → **RESOLVED by 0061** (README/CHANGELOG/docs bundled + reachable) |
| E2 authoring corpus — *indexing half* (semantic_search over reference docs) | → **de-scoped** (owner (i), 2026-07-13; `semantic_search` remains opt-in/functional) |
| E3 retrieval silent-degrade fix | ✅ done (#2895) |

**Loop (L):**

| item | disposition |
|---|---|
| L0 provenance split invariant `{builtin, user_directed, auto_improvement}` | ✅ landed (Layer A) |
| L1 promotion-as-idiom | → **de-scoped** (owner (i), 2026-07-13) |
| L2 eval-gated promotion (judge_output *as gate*) | → **de-scoped** (owner (i); judge_output itself landed + pipeline-invocable #2912 — only the promotion-gate idiom is de-scoped) |
| L3 catalog hygiene (usage-informed pruning) | → **de-scoped** (owner (i), 2026-07-13) |

**Open forks §7 — all now RESOLVED:**

| fork | disposition |
|---|---|
| (a) retrieval default-promotion | → **RESOLVED = keep opt-in** (owner reaffirmed `semantic_search` stays opt-in; floor is deterministic `reyn_repo`) |
| (b) E2 corpus shape (how to ship docs) | → **RESOLVED by 0061** (force-include bundle; not a distilled-guide) |
| (c) builtin exemplar curation | → **RESOLVED** (curated-5 landed) |

**Addenda:** A (feasibility) consumed; B (Layer A) / C (Layer C) landed; D
placement map + D9 present-affordance landed; **§D5b (docs mirror) →
superseded by 0061**; **§D10 (Stage-2 wielding eval) → owner DECLINED** (owner
judged the absolute-measurement necessity unclear and declined GO; recorded as
not-pursued, the plan text stays as a record).

**Phase roll-up:** Phase 0 ✅ · Phase 1 ✅ · Phase 2 ✅ · Phase 3 (E2 packaging
→ 0061; E2 indexing + docs-convention → de-scoped) · Phase 4 (L1/L2/L3 →
de-scoped). **0060 closes: floor+show delivered, enhancement/loop de-scoped.**

### 4.1 The only loose ends → RESOLVED = de-scoped (owner 2026-07-13)

After the above, **0060 had no in-flight work**; the sole undecided items were
the **opt-in-retrieval + loop enhancement layers** whose measurement trigger
(§5/D10 Stage-2) the owner declined:

- **E2 indexing** (semantic_search over reference docs — opt-in),
- **docs-convention ratification** (reference-vs-concepts frontmatter),
- **L1/L2/L3** (promotion idiom / eval-gated promotion / catalog hygiene).

**Owner decision (2026-07-13): option (i) — de-scope/close.** These enhancement
layers are **not pursued as 0060 deliverables**; 0060 closes clean with its
floor (F1-F4) + show (F3) layers delivered and **no HELD backlog**. This
de-scopes only the *unbuilt enhancement work* — the **landed foundations
remain** untouched: L0 provenance invariant, `judge_output` (landed +
pipeline-invocable), and `semantic_search` (opt-in, functional). Any future
auto-improvement / promotion / corpus-indexing is a **fresh proposal**, not
0060 backlog. The 0061 arc edits 0060 to mark these **de-scoped (not HELD)**
and sets 0060 status to **closed — floor+show delivered, enhancement/loop
de-scoped**.

**0061 preserves + upgrades** the F1 production-reachability invariant: from
"reference subset reachable" to "README + CHANGELOG + all docs + source
reachable, dev==wheel parity", `semantic_search` opt-in intact.

## 5. Sequencing

1. Hatchling migration (config enumerate→map→gate) + `force-include`
   (README/CHANGELOG/docs). Wheel-contents CI check.
2. `reyn_repo` resolver dual-mode (bundled-dir detection + normalization) +
   reachable-set SSoT + dev-allowlist derivation.
3. dev==wheel parity gate (positive + negative flip-witness); retire #2920's
   narrower gate into it.
4. Retirements (§3.5) — after the parity gate is green (so reachability never
   regresses through the swap).
5. Rename `reyn_src`/`reyn_source` → `reyn_repo` (live surfaces, journals
   excluded).

## 6. Testing

- **Parity gate** (§3.4): positive byte-identity dev↔wheel + negative
  non-declared-path refusal flip-witness + SSoT-strip → RED.
- **Config completeness**: wheel contains all package-data entries (the 3
  omission-risk items) + `_bundled/` tree.
- **Resolver mode detection**: bundled-dir-adjacent → wheel mode; dev checkout
  → dev mode; a `name=reyn`-ish unrelated tree does **not** get mis-resolved
  (confused-deputy negative).
- **Rename**: no live `reyn_src`/`reyn_source` refs remain (grep gate);
  journals untouched; ruff I001 clean.

## 7. Risks / open

- **Wheel size** +~few MB (all docs). Accepted per owner (full reachability).
- **`force-include` vs `project.readme` (long_description)** metadata — verify
  no collision (low risk).
- **mkdocs `docs_dir: ../docs`** unaffected (force-include does not move
  sources) — verify.
- **Open:** confirm the `_bundled/` layout name and whether source normalization
  (`src/reyn/X` ↔ `<pkg>/X`) should present `src/reyn/` or `reyn/` as the
  canonical logical prefix (pick one, apply in both modes).
