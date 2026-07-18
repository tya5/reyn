# 0064 — Plugin model for reyn (author → test → promote reusable capabilities)

- **Status**: Proposed (awaiting owner review)
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
3. **Promote (make reusable)** — package the working capability into a plugin: **copy → `~/.reyn/plugins/<name>/`, expand `${REYN_*}`, materialise its runtime deps (§3.11), then call the existing `mcp_install` / `pipeline install` / `skill install` verbs** to register whatever the plugin contains. Now it is reusable across sessions/projects. This *is* `plugin install`, sourced from local work.

> **Dep-fetch happens at install, never at spawn.** `uv run --with <deps>` fetches over the network; if that fetch is deferred to spawn, a `network:false` server can never start (the general form of #3060). So **install materialises a per-plugin env** (e.g. `uv venv` + install `<deps>` into `~/.reyn/plugins/<name>/.venv`, network available at install time) and the registered spawn command points at that ready env's interpreter — **spawn is network-free**. Detail in §3.11.

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

### 3.10 Security & permission (the capability surface of install)

Install is the plugin model's one place that touches new capability surfaces — it must pass the Security lens explicitly, not by omission.

- **Inherited (existing gates)**: the *register* step reuses the gates the existing verbs already carry (`require_mcp_install`, `require_file_write` on `.reyn/config/*.yaml`, etc.). No new gate there.
- **New surfaces that are OUTSIDE any existing gate — each needs an explicit gate**:
  1. **global-copy write** — writing `~/.reyn/plugins/<name>/` is a filesystem write *outside the workspace* (the default file-write gate is workspace-tight). Needs an install-scoped write permission.
  2. **dep materialisation = network + arbitrary PyPI fetch** (§3.11) — a network + package-fetch capability; gate it as such (and it is **install-time only**, so `network:false` at *run* time is unaffected). This install-time egress is a **reyn-originated network request** and is governed by reyn's **unified network policy** (proxy / CA-trust / SSL — the policy under design for all reyn egress): behind a corporate proxy the fetch obtains its proxy + CA from that policy rather than assuming a clean direct connection, so a materialise in a proxied/clean env still succeeds.
  3. **`{kind:git}` remote code = RCE trust boundary** — fetching and then *running* remote code is the highest-risk variant. It must be an **explicit operator-trust decision**, never auto-run. `{kind:builtin}` (reyn's own shipped) and `{kind:local}` (already on the operator's disk) carry lower trust risk; **the gate strength scales with `kind`** — builtin ≤ local ≪ git/remote.
- **Sandbox interaction**: the materialised per-plugin env is where a server's `network:false` / seccomp scope applies at *run* time (unchanged by this ADR); only *install* needs network. This keeps the loose-coupling promise (a server's runtime deps never touch reyn's env) *and* the sandbox promise (run-time network is still gate-controlled).

This section is the constitution pass-line the rest of the ADR was missing; nothing else here fails a lens.

### 3.11 Audit-event, atomicity & crash-recovery

Install/uninstall mutate durable state (a global copy + a materialised env + project registry entries) across several steps — they must be **observable and recoverable**, per the cross-cutting band.

- **Audit-events (P6)**: each install/uninstall emits audit-events — at minimum `plugin_install_started` / `_copied` / `_deps_materialised` / `_registered` / `_completed` (and the uninstall inverses) — so `reyn events` can reconstruct what landed and when.
- **Atomicity / reconcile**: the steps (copy → materialise deps → register) are not a single atomic write, so a crash mid-install can leave a **partial** plugin — copied but unregistered, or registered pointing at a half-materialised env. Install therefore needs a **reconcile** on next start: detect a `~/.reyn/plugins/<name>/` whose `_completed` event is absent (or whose registry entries/​env are inconsistent) and either finish or roll it back, so no half-installed plugin is left that is neither usable nor cleanly removable. Uninstall is ordered **drop-registry-first, then remove-copy** so an interrupted uninstall never leaves a live registry entry pointing at a deleted copy.
- **Not WAL-derived**: the `~/.reyn/plugins/` copies and the materialised env are **files**, not WAL-event-derived state, so the recovery-feature truncate-falsify gate (CLAUDE.md) does not apply to them; the reconcile above is a filesystem/registry consistency check, and the registry entries themselves ride the existing config-load path. (Called out explicitly so the distinction is on record.)

## 4. Consequences

- **#2955 turnkey resolved structurally**: capabilities run from a **per-plugin materialised env** under `~/.reyn/plugins/<name>/` (deps fetched once at install, §3.11) — no `pip install "reyn[builtin-rag]"`, no reyn-env coupling, no console-scripts, and **spawn is network-free** so a `network:false` server still starts. (`builtin-rag` extra survives only as a dev/test dependency for reyn's own direct-import unit tests, not the user launch path.)
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
