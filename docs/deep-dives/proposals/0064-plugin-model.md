# 0064 — Plugin model for reyn (author → test → promote reusable capabilities)

- **Status**: Accepted + Implemented — owner GO'd, all 5 phases (P1 manifest/token-expansion, P2 install machinery, P3 slash+CLI surfaces, P4 skill-load tool, P5 RAG plugin) landed and checked off in umbrella issue #3066 (CLOSED)
- **Date**: 2026-07-18
- **Arc**: grew out of the RAG turnkey arc (#2955)
- **Deferred (named, not decided here)**: agents/hooks (incl. event composition) plugin containment; hosting arbitrary third-party plugins; multi-version cache resolution; the end-user promote/install UX surface.

> Design contract (repo rule: spec/design lives in a doc, not PR comments/broker). "Standard behaviour" claims are backed by the Appendix research citations; "reyn already has X" claims by the §2 grounding (verified on `origin/main`).

## 1. Context & the primary use case

The RAG turnkey arc (#2955) began at the owner's stuck point — *"rag skill を呼んでもインストール方法がわからない"* — and resolved to a structural finding: reyn's builtin RAG **MCP servers** are shipped as **console-script entry points** binding reyn's interpreter, so using them needs `pip install "reyn[builtin-rag]"` **into reyn's env** — coupling an MCP server to reyn's environment, against MCP's loose-coupling premise. (`reyn_markitdown` is already loose: `uvx markitdown-mcp`.)

But the fix must serve the **actual daily use case**, which is *authoring*, not consuming pre-built plugins:

> **The LLM creates a stdio MCP server / a pipeline in-session, runs it to verify it works, and — if it works — makes it reusable across future sessions and projects.**

So the central operation is **promote a just-authored, just-tested capability into a reusable form** — closer to `git commit` / `npm publish` (packaging *your own* work) than to `npm install` (fetching someone else's). Installing a pre-existing third-party plugin is the *same mechanism from a different source*, and is secondary.

Two consequences shape everything below:

- A capability **starts life as a local dev artifact** (a `server.py` the LLM just wrote; an **inline** or local pipeline) and is **tested in that local form** before it is ever "installed."
- **A capability very often has no skill.** It is normal to build just a stdio MCP server, or just a pipeline, and never write a `SKILL.md`. Skills are **optional**, not the unit of packaging.

The owner's standing intents: loose-coupling (a capability's runtime deps must not pollute reyn's env); support the **industry-standard skill/plugin model** so standard skills/plugins run in reyn; a clean end-state with no migration debt, no reinvention, no brand-lock.

## 2. Grounding — what reyn already has (verified on `origin/main`)

- **Runtime-writable registries exist for ALL THREE capability types** (compile-time builtin dict + runtime `./.reyn/config/*.yaml`, same shape):
  - `mcp__install_local` → `.reyn/config/mcp.yaml`
  - `pipeline_management__install_local` / `__install_source` → `.reyn/config/pipelines.yaml`
  - `skill_management__install_local` / `__install_source` → `.reyn/config/skills.yaml`
  - All merged by `config/loader.py`; all in the hot-reload IN-set.
- **hot-reload is op-triggered (not fs-watch)** (`runtime/hot_reload.py`): `_INSTALL_SOURCE_SEAMS` maps `{mcp,pipeline,skill}_install` → their registry; **pure additions apply same-turn** (`apply_now`), same-name overwrites defer to the turn boundary. The OUT-set (security/permission/sandbox/budget in `reyn.yaml`) is **restart-only, structurally write-gated**.
- **Inline pipelines** already exist (`run_pipeline_inline`, skips the registry) — the "test it ad-hoc" plane for the authoring loop.
- **`~/.reyn` (user-global) is already in real use** (user `config.yaml`, `secrets.env`, `cache/`, `oauth_tokens.json`, …). The three capability registries are **project-scope only** (`./.reyn/config/`).
- `expand_env` (`security/secrets/interpolation.py`, ADR-0030) already expands `${VAR}` from **os.environ** across the whole MCP config (`op_runtime/mcp.py:34`) before spawn.

**Consequence**: the register substrate and the hot-reload seams the plugin model needs **already exist for all three types**. `.reyn/config/` is unchanged. The plugin model is a **copy + expand + orchestrate-the-existing-verbs** layer on top, plus a promote entry point.

## 3. Decision

### 3.1 A plugin is a self-contained directory — capabilities optional, any subset

Industry-standard shape (Claude Code / Cursor / Gemini CLI converge on "plugin = one bundle dir + manifest + typed subdirs"):

```
<plugin>/
├── .reyn-plugin/plugin.json   # manifest — declares WHICH capabilities are present
├── .mcp.json                  # (optional) MCP server declaration at plugin root
├── scripts/                   # (optional) bundled stdio server code, ref via ${REYN_PLUGIN_ROOT}
├── pipelines/                 # (optional) reyn-specific extension (declared as such)
└── skills/<name>/SKILL.md      # (optional) standard SKILL.md — honoured as-is
```

- **Every capability subdir is optional.** A valid plugin may be *just* an MCP server, *just* a pipeline, or any combination. Skills are the least-common member, never required. The manifest declares what is present; reyn registers exactly those.
- `skills/` and root `.mcp.json` follow the standard; `pipelines/` is a **declared reyn extension** (no standard equivalent).
- builtin RAG becomes the **first plugin** (dogfood): `builtin/plugins/rag/` shipped as the template.

### 3.2 The lifecycle: author (local) → test (local) → promote (reusable)

The primary flow, and where each existing mechanism plugs in:

1. **Author** — the LLM writes a stdio `server.py` (or a pipeline) into the working area. No packaging yet.
2. **Test (local, cheap)** —
   - MCP: register the local server against `.reyn/config/mcp.yaml` pointing at the working-copy path, exercise its tools. Loose-coupled from the start via `uv run --with <deps> python <path>` (isolated env, no reyn-env pollution).
   - pipeline: run it **inline** (`run_pipeline_inline`) or as a local `pipelines.yaml` entry.
3. **Promote (make reusable)** — package the working capability into a plugin: **copy → `~/.reyn/plugins/<name>/`, expand `${REYN_*}`, then call the existing `mcp_install` / `pipeline install` / `skill install` verbs** to register whatever the plugin contains. Now it is reusable across sessions/projects. This *is* `plugin install`, sourced from local work.

> **Register-only, no dep-fetch (superseded by #3209 — see §3.11b).** Install never provisions a plugin's external Python deps; that responsibility moved entirely to the installing skill's SETUP instructions + the operator/LLM's own venv. **Historical note (superseded)**: the original design instead had install itself materialise a per-plugin env (`python -m venv` + `pip install <deps>` into `~/.reyn/plugins/<name>/.venv`) so a `network:false` server could still start spawn-network-free (the general form of #3060) — §3.11b keeps that fail-fast/network-free-spawn property while moving *how* the venv gets there off of the registration op.

Installing a **pre-existing** third-party plugin bundle is the same copy+expand+register mechanism from a different source.

**Install entry points** (owner decision "A" — coexist, shared substrate):

- **`plugin install` / promote** — copy+expand a bundle (from local work or a source) + register its capabilities. Primary.
- **`mcp install` (external single server)** — coexists: adding one *external, referenced-not-copied* MCP server (`uvx markitdown-mcp`, a remote URL) is a first-class need plugins don't cover; no plugin ceremony for it.
- **No standalone `skill install` / `pipeline install`** as separate user surfaces: a lone skill/pipeline is a minimal plugin; pipelines also have the **inline** plane. (The underlying register verbs still exist and are what `plugin install` calls.)

Uninstall = remove the copy + remove the registry entries (see §3.7 provenance).

### 3.3 `.reyn/config/` is unchanged; scope = global code + project enable

- **`.reyn/config/{mcp,pipelines,skills}.yaml` works exactly as today** — same files, same loader/merge, same hot-reload seams, same project scope. The plugin model reuses it verbatim; only the *content* of an entry changes (e.g. an mcp entry's command becomes `uv run … python <resolved path>` instead of a console-script).
- **Scope** (matches Claude Code: single global code cache + project enablement): plugin **code** installs once to **global** `~/.reyn/plugins/`; **enablement/registration** is **project-local** in `./.reyn/config/` (the hot-reload plane reyn already has). "user vs project" governs *enablement*, not code location. Dev override (in-place from source, à la `--plugin-dir` / `pip install -e`) is the escape hatch for editing plugin code locally — and is effectively what "test the working copy" in §3.2 already is.

### 3.4 Variable expansion — split by variable *kind*, uniform across all capabilities

All three capabilities take **dynamic parameters** — MCP tool args + spawn `${env}`; pipeline `ctx` params; skill body vars — so timing is **not per-capability**. It splits by *variable kind*, applied identically to mcp config, pipeline, and skill body:

| variable kind | examples | expanded | applies to |
|---|---|---|---|
| **stable location** | `${REYN_PLUGIN_ROOT}`, `${REYN_SKILL_DIR}` | **at copy/install** — baked into the copied files | mcp / pipeline / skill (uniform) |
| **dynamic param** | `${REYN_PROJECT_DIR}`, `${env:VAR}`, per-run `ctx` | **at invocation** — mcp spawn env (`expand_env`), pipeline `ctx` (expr evaluator), skill body (skill-load) | mcp / pipeline / skill (uniform) |

- **No asymmetry between capability types.** Location vars are fixed the instant the plugin is copied → resolved once at copy, *inside the per-plugin copy dir* (context unambiguous → the N-plugin "same root" collapse cannot occur; that only happens if a `${…_PLUGIN_ROOT}` token is written into a **shared merged** file). Dynamic params only have a value at invocation → left as tokens/params in the copy and expanded per use.
- So the copy has its **location vars baked** and its **dynamic-param tokens preserved** — the same treatment for an mcp `.mcp.json`, a pipeline yaml, and a `SKILL.md`.

### 3.5 The skill-load step (the skill's instance of invocation-time expansion)

Today a skill body is read raw with zero substitution — the only capability with no invocation-time expansion pass. When a plugin ships a skill, replace the raw read with a **skill-load verb**: read the SKILL.md (its location vars already baked at copy) and expand the remaining **dynamic** vars — `${REYN_PROJECT_DIR}`, `${env:VAR}`, `${CLAUDE_*}` alias — in the current context, exactly as mcp's `${env}` expands at spawn and a pipeline's `ctx` resolves at run. Returns the expanded body; also centralises skill loading for permission/audit. Skills are optional (§3.1), so this surface exists only when a plugin ships one; the RAG skill exercises no dynamic var.

### 3.6 Tokens: `${REYN_*}` canonical, `${CLAUDE_*}` alias at ingestion

`${CLAUDE_*}` is honoured by **exactly one host** (Claude Code); no other agent expands it; there is **no vendor-neutral token** (LSP/DAP/MCP define none). Adopting it verbatim brand-locks to a competitor and buys no portability.

- **Canonical**: `${REYN_PLUGIN_ROOT}` / `${REYN_SKILL_DIR}` / `${REYN_PROJECT_DIR}`, as a layer **distinct from** the os.environ `expand_env` (config env-injection ≠ plugin/skill location). Two different value sources: `${REYN_PLUGIN_ROOT}` / `${REYN_SKILL_DIR}` are **anchor** locations (the plugin's / skill's own copied dir) — the anchoring uses reyn's dual-mode `resolve_reyn_root()` (`runtime/reyn_repo.py`) to find reyn's *own* install for a builtin plugin; `${REYN_PROJECT_DIR}` is **not** `resolve_reyn_root()` (that resolves reyn-the-project, not the operator's project) — it is a *dynamic* per-invocation value carried from the live session's workspace root and threaded through the expansion context (`PluginTokenContext`), matching §3.4's stable-location-vs-dynamic-param split.
- **Alias `${CLAUDE_*}`** only in the code path that ingests a Claude-authored SKILL.md/plugin. Preserve the `SKILL_DIR` vs `PLUGIN_ROOT` distinction.
- The **SKILL.md format** is the one genuine open standard (agentskills.io; adopted by Codex/Gemini/Cursor/Copilot) — honour as-is.

### 3.7 Provenance for clean uninstall (small additive field)

To uninstall a plugin cleanly, each registry entry it created should carry its **plugin id** (an additive field in the existing `.reyn/config/*.yaml` entry — not a schema/mechanism change). Uninstall = remove the `~/.reyn/plugins/<name>/` copy + drop entries tagged with that plugin id. This is the only change to what gets written into `.reyn/config/`.

### 3.8 LLM-facing install — one typed op, a discriminated source

reyn prefers **typed** over form-sniffed strings (Tool Contract lens: every side effect rides a typed, validated envelope — a Control IR / typed op — never an untyped string the LLM free-forms and reyn parses by shape). So `plugin_install` is **one op** whose `source` is a **typed discriminated union** (explicit `kind`), *not* a `plugin_install("<string>")` that reyn resolves by form:

- `{kind: "builtin", name: "rag"}` — reyn's shipped `builtin/plugins/<name>/`; the LLM passes only the stable name, reyn resolves its own location.
- `{kind: "local", path: "<dir>"}` — the local dir the LLM authored/tested, or a hand-written plugin. **Promote — the primary daily loop (§3.2) — is this variant.**
- `{kind: "git", url: "<url>"}` — remote (extensible to `registry`, etc.).

Every variant resolves to a source dir → the same **copy → `~/.reyn/plugins/<name>/` → expand → register**. The `kind` discriminator matches the typed-kind precedent already in the mcp surface (`mcp_install_package(kind, identifier)`), and avoids **both** the untyped form-sniffing string **and** the ad-hoc `name=` vs `source=` param split.

**Weak-model validation gate.** The existing mcp surface *split* install into separate verbs (`mcp_install_registry` / `_package` / `_local`) **not for aesthetics but because weak models got confused** choosing a `kind` variant + its fields. The union is preferred *only if* models handle it. Since reyn dogfoods with weak models, `plugin_install`'s discriminated union **must be validated against weak-model confusion** (a dogfood: does a weak model pick the right variant and fill the right fields?); if confusion recurs, the evidence-based fallback is the mcp-style separate-verb split — chosen on data, not taste. Ergonomics to reduce confusion: distinct self-explanatory `kind` values (`builtin`/`local`/`git`), per-variant typed fields, worked examples in the tool description, and `builtin` (bare name) as the simplest path.

`mcp install` (external single server, referenced-not-copied — e.g. `uvx markitdown-mcp`) stays a separate, lightweight entry (§3.2) — it is not a plugin and needs no copy.

### 3.9 Surfaces (LLM tool · slash · CLI) and uninstall

`plugin install` **and** `plugin uninstall` are exposed on **all three operator surfaces**, each a thin adapter over the **same typed op** (Control IR core — surfaces never re-implement the logic; Product Think lens: CLI/CUI affordance):

- **LLM tool**: `plugin_install(source={kind, …})` / `plugin_uninstall(name)`.
- **slash command**: `/plugin install …` / `/plugin uninstall <name>`.
- **CLI**: `reyn plugin install <kind> <arg>` / `reyn plugin uninstall <name>`.

The typed `kind` discriminator (§3.8) carries across every surface (CLI subcommand/flag, slash arg, tool field) — never a form-sniffed string.

**Uninstall** is the inverse of install: **drop the project registry entries tagged with the plugin id (§3.7) + remove the `~/.reyn/plugins/<name>/` copy.** Its lighter counterpart is a project-only **disable** (drop the entries, keep the global copy) — the inverse of *enable*, for turning a plugin off in one project while other projects keep it.

Implementation **mirrors how existing ops (e.g. mcp install / `reyn` CLI subcommands / slash commands) are already exposed across tool·slash·CLI** — grounded against those precedents, not a new surface pattern (grep them at impl time).

### 3.9a Discovery — `plugin_management__list` (#3202 symptom 3)

`plugin_install`'s `source={kind:"builtin", name:"rag"}` shape requires the
caller to already know the name `"rag"` — but until this addition, nothing
enumerated which builtin plugin names exist. The only path by which an LLM
could learn `"rag"` existed was a `rag_ingest`/`rag_query` pipeline call
failing at run time with an error message naming it — discover-by-failure,
a chicken-and-egg gap the manifest's own `description` + `capabilities`
(already complete, ADR §3.1) did nothing to close because nothing read them
before an install attempt.

The fix layers on top of the existing `.reyn-plugin/plugin.json` manifest
rather than duplicating it:

- **Registry** (`reyn.builtin.registry.BUILTIN_PLUGINS`, `src/reyn/builtin/registry.py`)
  — an explicit-dict **allowlist** of which `src/reyn/builtin/plugins/<name>/`
  directories are advertised (`{"rag": {"enabled": True}}`). No directory
  auto-scan — mirrors `BUILTIN_SKILLS`/`BUILTIN_PIPELINES`'s discipline and
  the #3196 rule that a directory appearing on disk must never itself
  advertise a capability. A CI gate
  (`tests/test_builtin_plugins_registry_disk_parity.py`) enforces two-way
  parity between this dict and the real `src/reyn/builtin/plugins/*`
  directories, so a new builtin plugin shipped without a registry entry
  fails CI instead of shipping silently undiscoverable.
- **`reyn.builtin.discovery.list_builtin_plugins()`** — reads the registry
  (which names to advertise) and DERIVES each one's `description` +
  `capabilities` live from its own manifest, rather than copying that text
  into the registry (copying would create the redundant-projection drift
  class #3164 hit for a different value — the registry answers "which", the
  manifest answers "what").
- **`plugin_management__list` LLM tool** (`src/reyn/tools/plugin_management_verbs.py`)
  — a read-only discovery verb with no Control IR op (mirrors `skill_list`,
  #2971: a pure enumeration has no side effect to gate). Reachable from the
  ordinary tool-call flow — an LLM can call it directly to answer "what can
  I install" without ever hitting an install error first.

`semantic_search`-based capability discovery (finding `"rag"` from a query
like "I want to search my PDFs" without already knowing its name) is a
separate, retrieval-lens follow-on — tracked outside this PR's scope; see
the PR body for the concrete decision.

### 3.10 Security & permission (the capability surface of install)

Install is the plugin model's one place that touches new capability surfaces — it must pass the Security lens explicitly, not by omission.

- **Inherited (existing gates)**: the *register* step reuses the gates the existing verbs already carry (`require_mcp_install`, `require_file_write` on `.reyn/config/*.yaml`, etc.). No new gate there.
- **New surfaces that are OUTSIDE any existing gate — each needs an explicit gate**:
  1. **global-copy write** — writing `~/.reyn/plugins/<name>/` is a filesystem write *outside the workspace* (the default file-write gate is workspace-tight). Needs an install-scoped write permission.
  2. ~~**dep materialisation = network + arbitrary PyPI fetch**~~ — **removed, #3209** (§3.11b): install no longer fetches deps at all, so this gate no longer exists on the install path. Historical note: the pre-#3209 design gated this as a network + package-fetch capability, install-time only.
  3. **`{kind:git}` remote code = RCE trust boundary** — fetching and then *running* remote code is the highest-risk variant. It must be an **explicit operator-trust decision**, never auto-run. `{kind:builtin}` (reyn's own shipped) and `{kind:local}` (already on the operator's disk) carry lower trust risk; **the gate strength scales with `kind`** — builtin ≤ local ≪ git/remote.
- **Sandbox interaction**: post-#3209, the operator's OWN venv (created per the installing skill's SETUP instructions, outside reyn's control) is where a server's `network:false` / seccomp scope applies at *run* time — install itself needs no network any more (§3.11b). This keeps the loose-coupling promise (a server's runtime deps never touch reyn's env) *and* the sandbox promise (run-time network is still gate-controlled).

This section is the constitution pass-line the rest of the ADR was missing; nothing else here fails a lens.

### 3.11 Audit-event, atomicity & crash-recovery

**Post-#3209 (§3.11b), read "materialised env" below as historical** — install
no longer materialises anything; the atomicity/reconcile shape (copy →
register, crash mid-way leaves a partial) is otherwise unchanged.

Install/uninstall mutate durable state (a global copy + project registry entries) across several steps — they must be **observable and recoverable**, per the cross-cutting band.

- **Audit-events (P6)**: each install/uninstall emits audit-events — at minimum `plugin_install_started` / `_copied` / `_registered` / `_completed` (and the uninstall inverses) — so `reyn events` can reconstruct what landed and when.
- **Atomicity / reconcile**: the steps (copy → register) are not a single atomic write, so a crash mid-install can leave a **partial** plugin — copied but unregistered. Install therefore needs a **reconcile** on next start: detect a `~/.reyn/plugins/<name>/` whose `_completed` event is absent (or whose registry entries are inconsistent) and either finish or roll it back, so no half-installed plugin is left that is neither usable nor cleanly removable. Uninstall is ordered **drop-registry-first, then remove-copy** so an interrupted uninstall never leaves a live registry entry pointing at a deleted copy.
- **Not WAL-derived**: the `~/.reyn/plugins/` copies are **files**, not WAL-event-derived state, so the recovery-feature truncate-falsify gate (CLAUDE.md) does not apply to them; the reconcile above is a filesystem/registry consistency check, and the registry entries themselves ride the existing config-load path. (Called out explicitly so the distinction is on record.)

### 3.11a Update 2026-07-21 — materialise moved from `uv` to stdlib `venv` + `pip` (#3202)

The original §3.11 design used `uv venv` + `uv pip install` for materialise, **with no rationale recorded anywhere for choosing `uv` over the stdlib alternative** — that absence is the actual root cause of #3202: `uv venv`'s POSIX-only venv-layout assumption (`<venv>/bin/python`, hardcoded in `plugin_install.py`) broke on Windows/git-bash, and because nobody had written down *why* `uv` was load-bearing here, nobody had grounds to question whether dropping it was safe when the bug surfaced.

**Recorded now, so the next person doesn't have to reconstruct it from a bug report**:

- **No lockfile was ever used** — materialise reads a plain `requirements.txt`, never a `uv.lock`. `uv`'s headline advantage (fast, reproducible, lockfile-pinned resolution) was never actually exercised by this call site.
- **Isolation is achieved by `python -m venv` alone** — the property this ADR actually needs (a per-plugin env, no reyn-env pollution) does not require `uv` specifically; the stdlib `venv` module (+ `pip`, bundled with every CPython) provides the same isolation.
- **reyn's own runtime already guarantees a working CPython** — `sys.executable` is always present or reyn itself couldn't be running. `uv`, by contrast, is an EXTRA binary an operator can lack (the reported failure: Windows/git-bash without `uv` on `PATH`, producing a confusing `run 'uv venv' to create environment` error for a tool reyn itself never asked the operator to install).
- **Ground before switching** (not assumed): a real `pip install` of the `rag` plugin's actual `requirements.txt` (including `sqlite-vec`, wheel-only, no sdist — the dependency most likely to need a resolver's special handling) resolved and installed cleanly with plain `pip`, no `uv`-specific behaviour lost.
- **Interpreter-path resolution** (the Windows-layout bug itself, #3202 symptom 1) is now a dedicated stdlib-`sysconfig`-based resolver (`_venv_interpreter_path` in `plugin_install.py`) — computed via `sysconfig.get_paths(scheme="venv", ...)`, which resolves the OS-appropriate layout internally (no hardcoded `bin`/`Scripts` branch at the call site), with an on-disk existence-check as a pathological-case fallback.

This is an **addendum, not a rewrite** of §3.2/§3.11's prose above — read those together with this note for the current mechanism.

### 3.11b Update 2026-07-23 — register-only: dep materialisation removed entirely (#3209)

**Owner-raised problem**: `plugin_install`'s dep-materialise step (§3.11/§3.11a) was a **foreign responsibility** — env-provisioning — riding a registration op. The whole #3202 arc (Windows venv-path bugs, `uv` vs stdlib `venv`, interpreter discovery, the pypi.org fetch-derive) was mechanism accumulating on top of a responsibility that never belonged on `plugin_install` in the first place. Architect-firm redesign, owner GO'd 2026-07-23: **install becomes registration-only.**

- **Removed, clean-break** (no transition shim): `_materialise_deps` (the `<sys.executable> -m venv` + `<venv_python> -m pip install` call), both interpreter-path resolvers (`_venv_interpreter_path` / `_venv_interpreter_path_discover`, §3.11a), the `_deps_materialised` install-state stage/audit-event, and the pypi.org dep-fetch permission derive (#3048's `session_approve_host("pypi.org", ...)` call site inside `plugin_install.py` — the general `PermissionResolver` mechanism itself remains, just with no caller here any more).
- **External deps are now skill-driven**: the `rag` plugin's two MCP servers (`chunker_server.py` / `vector_store_server.py`) are verified standalone (`import reyn` = 0 — they need no reyn code on `sys.path`), so the **lean venv needs only the plugin's `requirements.txt` deps** (`sqlite-vec`/`apsw`/`chonkie`/`fastmcp`), never reyn itself. The installing skill's SETUP instructions walk the operator/LLM through: create a venv → `pip install -r requirements.txt` inside it → point the plugin's `.mcp.json` server `command` at that venv's python interpreter **absolute path** (Windows: `Scripts\python.exe`). `plugin_install` registers whatever `command` the `.mcp.json` names **AS-IS** — no rewrite of any kind, for any platform.
- **pip moves to LLM-driven**: the operator's LLM runs the venv-creation + `pip install` itself, in-sandbox, following the skill — this is why #3207 (landed same arc) makes `SandboxPolicy.allow_subprocess` default `True`: without it, the LLM-driven pip install would itself need a subprocess grant the skill has no way to request generically.
- **Fail-fast preserved (#3060 by-construction, not weakened)**: a server whose registered `command` names an incomplete/missing venv fails at MCP spawn with a clear OS-level error (e.g. "no such file or directory") — `plugin_install`/spawn **never** falls back to fetching deps at spawn time to paper over it. The by-construction guarantee moves from "turnkey materialise" to "fail-fast + skill quality" — an explicit trade-off: turnkey install-time provisioning is traded for skill-guided user-managed venvs, in exchange for install carrying zero foreign env-provisioning responsibility.
- **Invariants unaffected**: registration/reconcile of `mcp`/`pipelines`/`skills.yaml` is unchanged (§3.7/§3.11's atomicity story shrinks by one step, not a different shape); WAL/recovery is unaffected (the materialised venv was never WAL-derived to begin with — see §3.11's "Not WAL-derived" note, now simplified since there is no venv to call out).

This update **supersedes §3.11a's interpreter-path-resolution mechanism** (dead — no caller remains) while **keeping §3.11a's own historical rationale intact** (why `uv` was dropped for stdlib `venv`+`pip` — that reasoning still applies to whatever venv-creation method the SETUP-instructions skill recommends to the operator/LLM, just no longer executed by `plugin_install.py` itself).

### 3.11c Update 2026-07-23 (same day) — venv location fix + inline SETUP steps, live e2e

Two independent live dogfood e2e runs against §3.11b's landed redesign validated register-only + fail-fast, but both found the pure-chat setup path stalling before the first query:

- **Venv location was wrong.** §3.11b's skill directed the LLM to create the venv at `~/.reyn/plugins/rag/.venv` — a HOME-dir path. But an LLM-driven `sandboxed_exec` call (`python3 -m venv ...` / `pip install ...`) runs under a `SandboxPolicy` whose `write_paths` floor is `[workspace.base_dir]` (`resolve_sandbox_policy`, `src/reyn/runtime/router_op_context.py`) — tight to the CURRENT PROJECT, not `$HOME`, and the LLM cannot widen it (`#1339`: the operator-or-default policy always wins over op-authored fields). A home-dir venv path is therefore OUTSIDE the LLM's write scope: `python -m venv ~/.reyn/plugins/rag/.venv` fails closed with "Operation not permitted", and the whole ingest/query flow silently never gets there — no operator keypress, no visible error surfaced back to the chat, just a stalled skill. A shared home-dir path is additionally GLOBAL across every project/session on the machine, so two unrelated projects racing the same materialise path collide (observed independently in testing).
  - **Fix**: the venv lives at a **workspace-relative path** instead — `./.venv-rag` at the current project's root (verified: `Path` grants in Seatbelt/Landlock are `(subpath ...)`-style, i.e. recursive under the granted `write_paths` entry, so ANY path under `workspace.base_dir` — including a project-root dir like `.venv-rag` — is writable with zero extra grant). The plugin's CODE copy is unaffected and stays at the global `~/.reyn/plugins/<name>/` (a DIFFERENT write authorized by `plugin_install`'s own `require_file_write` gate, not `sandboxed_exec`'s floor) — only the per-plugin VENV moves. `requirements.txt` is read from the global copy; the venv itself is created and lives entirely inside the project.
- **The setup steps lived in the wrong file.** §3.11b put the load-bearing SETUP steps in a separate `references/install-and-venv-setup.md`, but reyn's skill-load (`:build_and_query_rag_corpus`, `src/reyn/interfaces/skill_invoke.py`, #2971) delivers ONLY the SKILL.md body on invocation — a bundled reference is not auto-surfaced. A model invoking the skill by name never automatically saw the setup steps, had to separately decide to read the reference file, and (observed on a lighter model) sometimes picked the wrong read tool or hallucinated instructions instead — corrupting `args` into an invented `-m reyn_chunker` module form (there is none — `args` is a file path, untouched by this step) and using a bare `venv/` instead of `.venv-rag/`.
  - **Fix**: the load-bearing steps (create the workspace-relative venv → `pip install -r requirements.txt` → edit ONLY the registered `command`, never `args`) now live DIRECTLY in the SKILL.md body, with the "`command` only, never `args`" and "`.venv-rag`, not a bare `venv/`" points called out explicitly as the two observed failure modes. `references/install-and-venv-setup.md` is now supplementary detail only (the write-scope rationale, troubleshooting, markitdown's own fallback venv) — never load-bearing content the model must separately fetch to complete the flow.

Neither fix changes §3.11b's register-only contract itself (`plugin_install` still never touches deps) — both are corrections to WHERE the skill-driven venv lives and HOW its instructions reach the model, closing the pure-chat gap the live e2e runs surfaced.

## 4. Consequences

- **#2955 turnkey traded for skill-driven, #3209**: capabilities run from a **per-plugin, operator/LLM-created venv** (deps installed by the operator/LLM following the installing skill's SETUP steps, §3.11b — the plugin's CODE stays under the global `~/.reyn/plugins/<name>/` copy, but the venv itself lives INSIDE the project workspace, e.g. `./.venv-rag`: an LLM-driven sandboxed setup command cannot write outside the project's write scope, so a home-dir venv path fails closed and a shared home-dir path would also collide across projects) — no `pip install "reyn[builtin-rag]"`, no reyn-env coupling, no console-scripts, and **spawn is still network-free** (a server whose command names a ready venv interpreter needs no network to start; an incomplete venv fails fast instead of falling back to a runtime fetch). (`builtin-rag` extra survives only as a dev/test dependency for reyn's own direct-import unit tests, not the user launch path.)
- **The authoring loop is first-class**: author-local → test-local (mcp-install-local / inline pipeline) → promote-to-`~/.reyn/plugins/`. Skills optional throughout.
- **reyn becomes a standard-plugin host** (first step): SKILL.md open standard honoured; `${CLAUDE_*}` skills run via alias.
- **No new registries, no new hot-reload machinery** — the three registries + install seams already exist. `.reyn/config/` unchanged bar the additive provenance field.
- **CI**: removing console-scripts breaks `wheel-reachability.yml` (#2972); rewritten to exercise the `uv run` launch (same intent — launch works with reyn not importable by the child — proven more directly by uv isolation).

## 5. Rejected alternatives

- `pip install "reyn[builtin-rag]"` into reyn's env (status quo) — the coupling the owner rejected.
- Bake an absolute path into a **shared** config token — collapses N plugins to one root.
- Adopt `${CLAUDE_*}` verbatim — brand-locks, buys no portability.
- Fully unify install (external MCP as a thin plugin) — rejected for "A": keep a lightweight `mcp install` for external servers.
- Publish `reyn-rag-*` to PyPI — unnecessary; self-contained files run in place via `uv run` from the copied plugin dir.
- Require a skill per plugin — rejected: skills are optional; most authored capabilities have none.

## 6. Appendix — research citations (2026-07-18 web research)

- SKILL.md open standard: anthropic.com/engineering/equipping-agents-for-the-real-world-with-agent-skills; agentskills.io; adopters Codex/Gemini CLI/Cursor/Copilot.
- `${CLAUDE_*}` Claude-only; per-vendor tokens diverge; no LSP/DAP/MCP neutral token.
- Copy-into-managed-dir universal (`~/.claude/plugins/cache`, `~/.gemini/extensions`, `~/.vscode/extensions`, npm, pip, brew); dev in-place escape hatches (`--plugin-dir`, `-e`, `npm link`).
- Standard expands at **load** because it reads verbatim per-plugin files + has dynamic vars (claude-code issues #9427, #47789).
