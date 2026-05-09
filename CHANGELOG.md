# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog 1.1.0](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

<!--
  Release marker (rename, do not duplicate).
  At release time the maintainer renames the `[Unreleased]` header below to
  `## [0.1.0a2] — 2026-05-08` and inserts a new empty `[Unreleased]` block
  on top. See `docs/deep-dives/contributing/release-process.md`.
-->

## [Unreleased]

### Added

- `scripts/dogfood_long_session.py` long-lived-session driver (370 lines): CLI drives the A2A JSON-RPC endpoint with N consecutive prompts on the same agent, persisting history naturally (mirrors production); records empty rate by turn position, token counts, latency p50, JSON structured output (= `32d31b6`)
- `dogfood/scenarios/long_session_v1.yaml` — 7 scenarios covering research chains, pronoun followup, reference-back, multi-source compare, and repetitive context-growth stress (= `32d31b6`)
- `scripts/hn_research.py` — reusable industry-research pipeline: site-scoped DDG search → Algolia HN API per-item fetch → cross-thread digest. Concurrent fetch, /tmp cache, `--json` structured output; no new pip deps (= `a6c780f`)
- `src/reyn/chat/services/router_host_adapter.py` — RouterHostAdapter concrete implementation of RouterLoopHost protocol (681 lines); ChatSession's collaborators injected, RouterLoop has zero dependency on ChatSession internals; `@runtime_checkable` added to RouterLoopHost (= `21b6bf0`)
- `src/reyn/chat/services/memory_service.py` — MemoryService stateless service: memory_dir/memory_path path resolution, remember/forget/read_body ops; injects EventLog + 4 file callbacks; no OpContext/Workspace import (= `a5bd5d5`)
- `src/reyn/chat/services/budget_gateway.py` — BudgetGateway service: per-session budget bookkeeping (total_usage/cost, router_cap state, accumulate, check_and_increment_router_cap, pre-spawn gate, cost_line/budget_full/reset_all) (= `729befa`)
- Tier 2 invariant tests for RouterHostAdapter (4 tests, protocol conformance + delegation wiring), MemoryService (4 tests, round-trip + forget + layer mapping + events), and BudgetGateway; all use real instances, no mocks (= `21b6bf0`, `a5bd5d5`, `729befa`)
- `docs/concepts/skill-design-patterns.{md,ja.md}` — new concept doc cataloguing the 3 canonical skill shapes (Linear / Loop / Sub-skill composition), mixing-patterns paragraph, and 4 anti-patterns; placed near multi-agent.md in nav (= `d297939`)
- `docs/concepts/multi-agent.{md,ja.md}` — new "Four layers of multi-agent in Reyn" section: Layer 1 `@sub_skill` / Layer 2 `run_skill` / Layer 3 `delegate_to_agent` / Layer 4 `reyn mcp serve`, with ASCII diagram, per-layer table, and 4-line decision guide (= `d297939`)
- Phase execution sequence diagram in `docs/concepts/architecture.{md,ja.md}` — ASCII sequence (User/Agent/OS/LLM/Workspace/Events) showing context-build → LLM call → validation loop → Control IR → events → transition/finish/abort; placed before the act-sense-react lens section (= `d297939`)
- Plan-mode operator how-to (`docs/guide/for-skill-authors/use-plan-mode.{md,ja.md}`) covering when to use plan mode, trigger, `/plan list`, `/plan discard`, `/plan resume --from`, state persistence, operator intervention recipes, and common pitfalls (= `9d78ffe`)
- `web_search`, `web_fetch`, and `mcp` op-kind sections in `docs/reference/control-ir.{md,ja.md}` — fields verified against src/reyn/schemas/models.py; includes contributor sync rule and `OP_KIND_MODEL_MAP` pointer; same rule added to CLAUDE.md Hard NEVER block (= `9d78ffe`)
- README "How Reyn compares" section — 5-row comparison table (LangGraph / CrewAI / AutoGen / Semantic Kernel / Reyn) with Loop enforcement / State persistence / Replay / Strength columns; "fits when / does not fit when" lists; placed between Quick Start and Architecture (= `9d78ffe`)
- `docs/deep-dives/research/competitive/semantic-kernel.md` — 350-line Semantic Kernel competitive research note (10 primary sources cited, 13-row Reyn 対比 table, 7 capability gaps, §8–§9 sections); README SK row sharpened with specific claims backed by the research (= `7952f9d`)
- 5 mid-priority ja translations: `postprocessor.ja.md`, `skill-resume.ja.md`, `author-a-design.ja.md`, `reference/dsl/postprocessor.ja.md`, `reference/stdlib/read_local_files.ja.md` (750 lines total) (= `7952f9d`)
- Eval rubric "Evidence-bound" principle (§5) and "Adversarial self-check" section in `docs/guide/for-skill-authors/eval-builder-rubric.md`, citing Berkeley RDI Trustworthy Benchmarks paper; eval_builder `analyze_skill.md` + `write_eval.md` + `skill.md` hardened with negative-test requirement and evidence-bound audit (= `a6c780f`, `7952f9d`)
- "Reyn through the act-sense-react lens" section in `docs/concepts/architecture.{md,ja.md}` — maps loop steps (act/sense/re-act/loop closure) 1:1 onto Reyn primitives; cites Tines blog + HN discussion (= `a6c780f`)
- "Downstream tooling — what builds on Reyn" section in `docs/concepts/care-boundary.{md,ja.md}` — names 5 raw primitives Reyn exposes and 4 downstream product categories; frames events log + WAL as public-facing contracts (= `a6c780f`)
- G30 giveup-tracker entry: explicit decision NOT to add multi-agent debate primitive; HN expert consensus cited as supporting data; counter-argument triggers documented for future re-opening (= `a6c780f`)
- `docs/deep-dives/insights/2026-05-09-hn-ai-agent-landscape-insights.md` — 4 actionable insights from 10 HN AI-agent threads (2025-2026) via Algolia API cross-thread analysis (= `9e04c04`)
- `docs/deep-dives/journal/sessions/2026-05-09.md` — session chronicle: 1 HN query → events-log discipline rescue → 1-line description hint → 4 insights → parallel multi-axis landing → reusable industry-research tooling (= `72364fe`)
- `docs/deep-dives/journal/dogfood/2026-05-09-long-session-baseline/` — baseline measurement findings (212-line chronicle) and raw driver output; G28 giveup-tracker entry extended with measured data: 37-turn baseline, 2% empty rate, cold-start dominated (= `5b47827`)
- Section 6.6 "Long-lived session pattern" in `docs/deep-dives/contributing/dogfood-discipline.{md,ja.md}` (+83 lines each) covering driver design, when-to-use comparison table vs. per-run clean_state vs. plan-mode Class 3, and known limitations (= `5b47827`)
- Web A2A endpoint subsection in dogfood-discipline section 6 (script-friendly debug surface): curl one-liner, list-agents, send-message, delineation from trace/replay tools (= `cf9d193`)
- `write-your-first-custom-skill.{md,ja.md}` how-to in `docs/guide/for-skill-authors/` — step-by-step from scratch via skill.md / phases/<name>.md / artifacts/<name>.yaml with `react_to_text` worked example, common P1/P8 mistakes, stdlib pointers (= `2c56577`)
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

- Guide nav restructured: `agent-engineering/` moved from `guide/` to `concepts/agent-engineering/` (conceptual essays belong under Concepts, not Guide); `for-skill-authors/` nav split into 6 task-type clusters (Foundation / Composition & multi-agent / Phase mechanics / Operations / UX & polish / Working with stdlib tools) — file paths unchanged, nav grouping only (= `2c56577`)
- Getting Started reordered: chat-mode tutorial promoted from position 05 to 02 (value-first onboarding — users see Reyn work before authoring); build → run → eval dependency chain preserved in positions 03-05; stale "Phase 2" cross-references corrected to live links (= `4684a90`)
- Tutorial 02 refocused on the auto-created `default` agent only: `reyn chat researcher` command removed (researcher agent doesn't exist by default); multi-agent section (`reyn agent new`, `/attach`, delegation, topology) cut and forwarded to `build-an-agent-team` how-to; example query "what skills are available?" replaced with "what is this project about?" (verified live against A2A endpoint) (= `80d649b`, `563ace6`)
- `web_search` router tool description extended with search operator hints: `site:<domain>`, `"phrase"`, `-term` surfaced as available capabilities, phrased as option not MUST rule (care-boundary compliant); 4 LLMReplay router fixtures re-recorded due to tools-array hash change (`chitchat_text_reply`, `invoke_skill_single_round`, `memory_recall_via_list_then_read`, `named_skill_direct_invoke`) (= `8af3444`)
- `reyn web --reload` recommended as the standard dev-mode server start in dogfood-discipline section 6 (replaces plain `reyn web`); memory entry updated so future sessions use `--reload` by default (= `b465521`)
- `docs/reference/control-ir.{md,ja.md}` gains contributor sync rule: new op kinds MUST be documented in the same PR; CLAUDE.md Hard NEVER block updated reciprocally (= `9d78ffe`)
- `eval_builder` `skill.md` finish_criteria gains "the generated eval includes at least one negative-test case" requirement (= `7952f9d`)
- session.py reduced −449 lines (~12%) over wave 3 (3858 → 3409): BudgetGateway −7 net (PR1), MemoryService −74 (PR2), RouterHostAdapter −368 (PR3); five memory methods become 1-line delegators (= `729befa`, `a5bd5d5`, `21b6bf0`)
- Router system prompt — category-only catalog, O(1) skill scaling, lazy item discovery (= `f4c5df2`)
- Router V3 ABSOLUTE routing rule + JA examples (= `d44841e`)
- Router direct invoke when skill name appears in Available skills (= `d07fa3c`)
- Router `describe_skill` field stripped to eliminate G12 Pattern D attractor (= `4c2965a`)
- Router skill description truncated in `list_skills` + system prompt (= `f781836`)
- LLM post-tool empty-stop workaround — inject `(answered)` trailing user (envelope-layer fix for G12 Pattern E) (= `aab6be2`)
- `dsl/` legacy path retired; `examples/` renamed to `cookbook/` (= `edcccbd`)
- `dsl_root` identifier renamed to `skill_root` (= `be3ee3f`)
- **Docs site URL structure**: migrated from `/en/` `/ja/` language-prefixed paths to suffix-based i18n (= en at root, ja at `<file>.ja.md`). Internal documentation (= journal / research / decisions / contributing / spec) consolidated under `deep-dives/` and excluded from public site. External links to `tya5.github.io/reyn/docs/en/...` URLs no longer resolve (= P1+P2+P3 restructure landed pre-PyPI).

### Fixed

- Tutorial 02 blocker: `reyn chat researcher` command referenced an agent that doesn't exist by default; removed in favour of `reyn chat` (default agent) (= `80d649b`)
- Tutorial 02 example query "what skills are available?" returned a conversational ask-back instead of a list; replaced with "what is this project about?" (verified live) (= `563ace6`)
- Tutorial 02 stale "Phase 2" labels in Next-step pointers for tutorials 03 and 04 corrected to live links (= `4684a90`)
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

- Public budget/cost reference page (`reference/config/budget.md`, en + ja) — `cost:` schema, slash commands (`/cost` / `/budget` / `/budget reset`), cap tiers, events, ledger, known limitations
- Public CLI reference for `reyn mcp serve` (`reference/cli/mcp.md`, en + ja) — flags, tools exposed, exit codes, Claude Code wiring example
- Dogfood session chronicle 2026-05-09: HN research → description hint → 4 insights → parallel landing (= `72364fe`)
- Long-session baseline findings journal + raw driver output (G28 confirmed driver-induced; true rate ~2% at N=37) (= `5b47827`)
- HN AI-agent landscape insights doc (2025-2026) — 4 actionable findings from 10 threads (= `9e04c04`)
- Plan-mode concept docs (en + ja) (= `891f0fd`)
- Dogfood discipline guide — 9 原則 + patterns + tools (= `82fd95e`)
- Care boundary concept — what Reyn cares vs observes (en + ja) (= `527f702`)
- Powered by AI transparency disclosure — LP footer + README + docs (= `8f3e2ad`)
- README live URLs + pyproject Homepage + docs-header GitHub link (= `e554b8a`)
- Reyn brand applied to mkdocs-material (= `9aaa639`)
- Docs dark code blocks, header nav links, sidebar section markers (= `172725c`)
- `permission-model` — `reyn.local.yaml` dogfood pre-approval pattern (= `3f1d1a5`)

### Removed

- `tests/scaffold/test_session_router_helpers.py` deleted per scaffold lifecycle policy (`removed_by` metadata pointed at the ChatSession-extraction PR — wave 3 PR3 is that PR) (= `21b6bf0`)
- `Phase.permissions` field — replaced by skill-level permissions per ADR-0020 (= `3dab751`)
- TUI screenshots from repo root + gitignore the pattern (= `990c139`)
- Real-looking secret patterns scrubbed from test fixtures + docs (= `26fe398`)
- `Wave A` strip-inline-catalog + `discover_tools` (reverted) (= `589e50f`)
- `stdlib_root()` default read zone addition (reverted as doc-violating) (= `3c3db08`)
- Non-interactive `startup_guard` auto-approve (reverted as doc-violating) (= `70257f2`)

[Unreleased]: https://github.com/tya5/reyn/compare/v0.1.0a2...HEAD
[0.1.0a2]: https://github.com/tya5/reyn/releases/tag/v0.1.0a2
