# FP-0034 dogfood batch progression plan (wrapper-only e2e)

> FP-0034 (universal catalog routing) Phase 1-3 landing 後の dogfood 進行計画。
> **Wrapper-only e2e** で測定 (= `hide_legacy_tools=True`)。 ship する Phase 5 後の state を直接測る。
> 3 経路並存 transitional state は dogfood scope 外 (= code landing safety のみ、 product state でない)。
> Phase 4-6 (= SP refactor / legacy deprecation / cleanup) は data-driven に go/no-go 判断。

---

## Goal hierarchy

**Final milestone**: "FP-0034 production grade landed" — **N=5 で ≥80% verified across 4 core scenarios**

**Intermediate milestones**:
- **M0**: ~~B23-PRE-1 SP misalignment fix (= Phase 4 preview, pre-batch 23)~~ ✅ **完了済** (= SP radical simplify ~2500 chars、 3-way intent routing + tool descriptions absorb、 commit `<TBD-after-agent-completion>`)
- **M1**: Functionality verification — 「 wrapper-only routing が動く」 (batch 23-24)
- **M2**: Attractor surface + fix wave (= B22 pattern) (batch 25)
- **M3**: N=5 stability ≥80% verified (batch 26)
- **M4**: Phase 5 default flip + Phase 6 cleanup go (batch 27)

---

## Pre-batch 23: B23-PRE-1 SP fix (= Phase 4 preview) ✅ 完了済

**Source**: Agent 6 trace deep-dive — SP の Capabilities / Behaviour section が legacy (= `invoke_skill` / `list_skills`) を primary routing と記述、 `invoke_action` 言及なし

**Completed**: SP を radical simplify (~2500 chars)。 旧 Capabilities / Behaviour section を廃止し、 **3-way intent routing** (= `search_actions` / `list_actions` / `invoke_action`) を top-level に昇格。 各ツールの routing rule は tool descriptions に migrate (= multi-layer reinforcement)。

**Result**:
- 旧 SP の legacy-primary 記述を完全除去
- `invoke_action` を primary call path として明示
- tool descriptions が routing authority を持つ構造に移行 (= Lever C/D multi-layer)
- commit `<TBD-after-agent-completion>`

**Actual wall-clock**: ~1-2 hours (5 sonnet parallel context + 1 commit)

---

## Batch progression

### Batch 23 — Practice / Calibration (wrapper-only, N=1)

**Goal**: wrapper-only e2e infrastructure 通過確認 + Brier baseline + B23-PRE-1 fix effectiveness の verification

**Scenarios (3, N=1)**:
- S1: Catalog discovery (3-turn) — list_actions → describe_action → invoke_action
- S2: routing_decided P6 event emit
- S3: exec visibility gating

**Expected Brier**: 0.3-0.5
**Wall-clock**: ~0.5 hours

**Gates → batch 24**:
- ≥2 scenario で `routing_decided` emit 観察 (= infra 通過)
- CRITICAL finding ゼロ (= 4 wrapper unhandled tool エラー、 hide_legacy_tools regression 等)
- B23-PRE-1 fix が blocked rate ≤ 15% を達成

### Batch 24 — Core path verification (N=3)

**Goal**: wrapper-only routing path を N=3 で functionality 確認、 base rate 確立

**Scenarios (4-5, N=3)**:
- S1-extended: catalog discovery via `list_actions(category="skill")`
- S4-hot-list-cold: hot list が freq=0 から start で direct alias 呼出 rate を測定
- S4-hot-list-warm: 同 skill 5 回呼出後、 hot list direct alias で呼ばれる rate (= ActionUsageTracker accumulation)
- S5-search-actions: P-natural prompt で semantic search → invoke (= embedding 必須)
- S6-mcp-via-wrapper: mcp.tool__brave.search (if configured) via `invoke_action`

**Expected Brier**: 0.25-0.45
**Wall-clock**: ~1.5 hours

**Gates → batch 25**:
- core 3 scenarios で verified+inconclusive ≥ 65%
- CRITICAL finding ゼロ
- hot list warm path で direct alias 呼出 rate >0%

### Batch 25 — Attractor surface + fix wave

**Goal**: 観察された attractor を Class A/B/C taxonomy で分類、 multi-layer reinforcement fix (= B22 pattern) を 1 commit で land

**Pre-fix multi-agent context analysis** (= 原則 16):
- attractor が surface したら fix の前に 5 axis context analysis
- batch 22 evidence: 4 attempts prompt-tweak 0% → 1 attempt context-driven 100%

**Scenarios (3-4, N=3-5)**:
- batch 24 で surface した attractor の確認 retest
- new scenario: hot alias description quality test (= description rewrite effectiveness)
- new scenario: P-explicit vs P-natural の base rate gap (= 同 query を 2 class で測る)
- fix wave 後の retest (= verified+inconclusive ≥80% confirm)

**Expected Brier**: 0.15-0.30
**Wall-clock**: ~2-2.5 hours

**Gates → batch 26**:
- attractor が surface しなかった場合は batch 25 を skip して batch 26 に直接進む
- attractor が surface した場合は fix wave 後 verified ≥75%

### Batch 26 — N=5 stability (production grade trial)

**Goal**: core 3-4 scenarios で N=5 達成し ≥80% verified を計測

**Scenarios (3-4, N=5)**:
- core scenarios の N=5 拡張
- batch 25 fix の verify (= multi-layer reinforcement が N=5 で 100% compliance か)

**Expected Brier**: 0.10-0.25
**Wall-clock**: ~2 hours

**Gates → M3 production-grade**:
- core 3 scenarios で N=5 ≥80% verified
- `routing_decided` 全 run で emit (= P6 structural requirement)
- CRITICAL / HIGH finding ゼロ
- B22 multi-layer fix が landed していれば N=5 で 100% compliance 確認

### Batch 27 — Phase 5 default flip + Phase 6 cleanup go/no-go

**Phase status note**:
- **Phase 4** (= SP §D9 category-only refactor): B23-PRE-1 の multi-layer reinforcement (= SP simplification + tool description migration) で **substantially landed**。 残: legacy path の同様 cleanup (= dogfood で wrapper-only の N=5 stability 確認後)
- **Phase 5** = `hide_legacy_tools=True` default flip (= `reyn.yaml` default change の 1 line PR)
- **Phase 6** = legacy 21 件 tools の物理削除 (= per-kind tool .py files の deprecation marker → 削除)、 BM25Backend / Anthropic tool_search_tool / shell op cleanup

**Goal**: 
1. `hide_legacy_tools=True` を default に flip (= Phase 5)
2. legacy tool 21 件のうち unused なものを削除 (= Phase 6 cleanup)
3. Track 2 (= legacy-only) spot check で regression なし confirm

**Scenarios (2-3, N=3 + Track 2 N=1-2)**:
- S12-phase5-default-flip: `hide_legacy_tools=True` default で既存 e2e 完走
- S13-phase6-cleanup-confirm: legacy 削除済 commit で全 test suite green + dogfood scenarios 動作
- Track 2-legacy-only: regression sanity (= 既存 e2e が backwards-compat path で動作するか)

**Gates for Phase 5+6 go**:
- S12 verified ≥66% (= N=3 中 2 件)
- Batch 26 で ≥80% verified 達成済
- Track 2 で regression なし

**Gates for Phase 5 only (= 6 defer)**:
- S12 verified ≥66% だが legacy 削除に既存 dependency
- Phase 5 のみ land、 Phase 6 削除は別 PR で incremental

**Gates for both defer**:
- S12 / S13 で blocked / refuted 多数
- B22 fix が N=5 で 100% compliance 未達

---

## Risk areas

| Risk | Source / Status | Mitigation |
|---|---|---|
| ~~**SP misalignment** (= B23-PRE-1)~~ | ✅ **解消済** (= refactor 完了、 3-way intent routing + tool descriptions absorb) | — |
| **Tool description over-pinning** | B23-PRE-1 で Lever C/D に routing rule migrate — description 過剰 assertive cue で逆 attractor 生む可能性 | batch 23 で base rate 測、 over-assertive なら description wording を緩和 |
| **Hot list direct alias 利用 rate** | freq=0 start、 短 session では direct alias 呼び rate 低 | batch 24 で usage tracker accumulation 後の rate 測 |
| **Permission communication design** | option B (trial-and-error) で運用、 wasted call rate を batch 23 で測 | FP-0035 (= 別 issue) で best pattern adopt |
| **Hot alias description weakness** | Agent 4 + 7 industry research | batch 24 で direct alias rate 測定後、 multi-layer description rewrite |
| **Class B affordance-bias** between wrappers (= list_actions vs search_actions) | Agent 7 prior art | batch 24 で base rate 測定、 B22 fix pattern 適用 |
| **Position bias** (δpos 0.168-0.443) | arXiv 2510.00307 | hot list は intentional prefer なので feature、 ただし legacy-like attractor ありえる |
| **Embedding 未設定で search_actions 不在** | D14 visibility gate | batch 23-26 で embedding 必須、 batch 27 で no-embedding fallback path 確認 |
| **Wiring gap = attractor 区別不能** | B17 TP2 | 各 prelude で structural pre-check (= 原則 10) 実施 |
| **Monotonic improvement 仮定** | B19 self-audit lesson | batch 25 で backward step ありえる、 taxonomy downgrade 想定 |

---

## Calibration estimate

| Item | Value |
|---|---|
| Pre-batch 23 (B23-PRE-1 fix) | ~1-2 hours |
| Total batches | 5 (23-27) |
| Total wall-clock | ~7-9 hours |
| Total scenario runs | ~50-70 |
| 5 sonnet 並列前提 | 1 batch ~1-2.5 hours |

後続 batch は早期 findings から redirect 可能:
- batch 24 で attractor 0% → batch 25 を skip、 batch 26 直接
- batch 25 fix wave 後 N=5 ≥80% → batch 27 で Phase 5+6 go decision
- monotonic improvement 仮定なし — batch 25 で backward step ありえる、 self-audit で taxonomy downgrade も想定

---

## Cross-referenced FP issues

- **FP-0034** (#36): universal catalog routing (= 本 plan の対象)
- **FP-0035** (#`<TBD>`): Sandbox / Permission LLM Communication Design (= B23-PRE-1 で Files permission scope drop の follow-up FP)
- **FP-0017** (#18): Sandbox backend abstraction (= sandboxed_exec の declarative permission pattern reference)
- **FP-0019 / 0020 / 0021**: session.py refactor 系 (= 完了済、 plan に影響なし)

---

## Cross-references

- `prelude.md` (batch 23): 詳細 scenarios + 4-dim audit + 5-axis context analysis
- `docs/deep-dives/contributing/dogfood-discipline.ja.md`: 9 原則 framework
- `docs/reference/dogfood-tracing.md`: trace infra tooling
- FP-0034 issue #36: D1-D24 design decisions
- 直近 batches: batch 17-22 retrospective (= Brier trajectory + attractor taxonomy 確立)
- B23-PRE-1 fix commit: `<TBD-after-agent-completion>` (= SP radical simplify + 3-way intent routing)
- FP-0035 (= 並行 agent で起票予定、 Permission communication design follow-up)
