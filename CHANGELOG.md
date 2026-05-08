# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog 1.1.0](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

<!--
  Release marker (rename, do not duplicate).
  At release time the maintainer renames the `[Unreleased]` header below to
  `## [0.1.0a2] — 2026-05-08` and inserts a new empty `[Unreleased]` block
  on top. See `docs/en/contributing/release-process.md`.
-->

## [Unreleased]

### Added

- Plan-mode crash resilience Phase 1 — fail-safe + observability (= `5f4944a`)
- Plan-mode forward replay Phase 2 — 7-step migration with PlanRegistry, analyzer, coordinator, runtime, dispatch wiring, auto-resume (= `bcf1105`〜`1e529d7`)
- Plan-mode Phase 2.1 — async dispatch + multi-plan + per-plan chain_id (= `a13caaa`)
- Plan-mode `/plan list` / `/plan discard` slash commands + WAL truncation floor (= `b1da83b`)
- Plan-mode `/plan resume --from <step_id>` slash command (= `619b2ad`)
- Per-plan step result spill-to-file for large artifacts (= `80e4977`)
- Sub-loop LLM call memoization (= `4cc764c`)
- Plan-mode tool — decompose complex queries into narrow per-step LLM calls (= `6b41fd0`)
- Multi-plan crash + resume integration tests (= `4912457`)
- ModelSpec passthrough for per-class LLM kwargs (= `063f036`)
- Model class extends + 8-entry built-in catalog (= `f0d5a56`)
- Time-travel debug replay walker + diff (= `0e82d5c`)
- Multi-file replay/compare (= `a501edd`)
- TUI right-panel with tabs (keys/events/agents/memory/docs) (= `bc30dfd`)
- TUI events panel — color codes, inline hints, filter/tail cycling (= `58c22d7`)
- TUI cost tab with today/all-time/by-model token aggregation (= `b0166bd`)
- TUI live skill/phase execution state in Agents tab (= `ec382c2`)
- TUI multi-line TextArea input + screenshot via Ctrl+\ (= `2a73606`)
- TUI inline SlashPicker (Discord/Slack-style autocomplete) (= `119b52a`)
- TUI neofetch-style banner + ASCII art logo (= `881b98c`, `8e58b52`)
- TUI banner opt-in via `--banner` flag (= `f641720`)
- A2A (Agent2Agent) protocol surface — peer-addressable Reyn agents (= `bea4d73`)
- Reyn agents exposed as MCP server (= `4d92a78`)
- MCP-over-SSE on FastAPI + 82% asset payload reduction (= `efb223f`)
- HN-friendly first-touch UX — identity, project context, files, memory (= `e80ae4b`)
- `reyn_src_*` tools — agent reads Reyn's own repo (= `f5c88ab`)
- `file.read` default + `web_search` / `web_fetch` tools (= `609a334`)
- `discover_tools` (replaced by category-only catalog, see Changed) (= `dc8296f`)
- Postprocessor adopted in 3 arithmetic-heavy stdlib skills (= `0a7e064`)
- `dogfood-trace` plan-summary / plan-trace / plan-snapshot modes (= `f4952af`)
- `llm_replay --from-attractor` for end-to-end observation cycle (= `2bb93e7`)
- LLM trace dump production hardening — size rotation + secrets redaction (= `7ecaee4`)
- Provider-specific response field capture for empty-stop diagnosis (= `9715ad5`)
- Router empty LLM response detection + event + explicit failure UX (= `0d624de`)
- Router `list_skills` exposes input artifact + fields hint (= `a38e0fb`)
- GitHub Pages workflow — landing page at `/`, docs at `/docs/` (= `7fc266e`)
- Website landing page from Claude Design v1 (= `946cb1a`)
- CODE_OF_CONDUCT.md + OSS Lv.1 finalisation (= `18274b6`)

### Changed

- Router system prompt — category-only catalog, O(1) skill scaling, lazy item discovery (= `f4c5df2`)
- Router V3 ABSOLUTE routing rule + JA examples (= `d44841e`)
- Router direct invoke when skill name appears in Available skills (= `d07fa3c`)
- Router `describe_skill` field stripped to eliminate G12 Pattern D attractor (= `4c2965a`)
- Router skill description truncated in `list_skills` + system prompt (= `f781836`)
- LLM post-tool empty-stop workaround — inject `(answered)` trailing user (envelope-layer fix for G12 Pattern E) (= `aab6be2`)
- `dsl/` legacy path retired; `examples/` renamed to `cookbook/` (= `edcccbd`)
- `dsl_root` identifier renamed to `skill_root` (= `be3ee3f`)

### Fixed

- G27 A2A async-dispatch return mismatch — plan terminal text now reaches caller (= `3a59d8c`)
- Plan-mode 2 dogfood bugs — `_PlanStepHost.resolve_model` + step recursion guard (= `ea97509`)
- Plan tool description — disambiguate `step.tools` field (= `7d0d6a2`)
- `run_skill` uses proxy model class instead of literal string (= `9bcba46`)
- Preprocessor permissions — extend default read zone to include `stdlib_root()` (= `59b57dc`)
- `skill_improver` preserves `_resolved_paths` through `copy_to_work` decide turn (= `dfa6b35`)
- `skill_improver` allows read access to stdlib skill paths in `copy_to_work` (= `a2f82f8`)
- `eval_builder` `_extract_skill_name` top-level `target_skill` (= `c5f67bc`)
- `eval_builder` clarifies routing distinction from eval skill (= `1ed7ecc`)
- `eval_builder` handles unknown `artifact_type` (= `e57399d`)
- `_build_history_for_router` head/tail overlap duplication (= `f4d71f3`)
- Drop duplicate trailing user message in RouterLoop messages (= `3732275`)
- MCP — `_handle_user_message` driven inline + `scripts/mcp_probe.py` (= `a5678c1`)
- MCP — require positive reply signal before declaring agent idle (= `b535517`)
- MCP — chdir to project_root so deep relative `.reyn` paths resolve (= `bb0162c`)
- MCP — hard-fail when no project root + document `--project` requirement (= `0711a98`)
- CI — install `[mcp,web]` extras alongside `[dev]` in pytest job (= `bee4762`)
- CI — add `pytest-asyncio` dev dep, narrow ruff rules (= `79d08e5`)
- Path-traversal test widened + cascade attractor revert note (= `f302099`)
- Various TUI focus / scroll / keybinding fixes (= `c66be03`, `55c3ea8`, `e88fb6f`, others)

### Documentation

- Plan-mode concept docs (en + ja) (= `891f0fd`)
- Dogfood discipline guide — 9 原則 + patterns + tools (= `82fd95e`)
- Care boundary concept — what Reyn cares vs observes (en + ja) (= `527f702`)
- Powered by AI transparency disclosure — LP footer + README + docs (= `8f3e2ad`)
- README live URLs + pyproject Homepage + docs-header GitHub link (= `e554b8a`)
- Reyn brand applied to mkdocs-material (= `9aaa639`)
- Docs dark code blocks, header nav links, sidebar section markers (= `172725c`)
- `permission-model` — `reyn.local.yaml` dogfood pre-approval pattern (= `3f1d1a5`)

### Removed

- `Phase.permissions` field — replaced by skill-level permissions per ADR-0020 (= `3dab751`)
- TUI screenshots from repo root + gitignore the pattern (= `990c139`)
- Real-looking secret patterns scrubbed from test fixtures + docs (= `26fe398`)
- `Wave A` strip-inline-catalog + `discover_tools` (reverted) (= `589e50f`)
- `stdlib_root()` default read zone addition (reverted as doc-violating) (= `3c3db08`)
- Non-interactive `startup_guard` auto-approve (reverted as doc-violating) (= `70257f2`)

[Unreleased]: https://github.com/tya5/reyn/compare/v0.1.0a2...HEAD
[0.1.0a2]: https://github.com/tya5/reyn/releases/tag/v0.1.0a2
