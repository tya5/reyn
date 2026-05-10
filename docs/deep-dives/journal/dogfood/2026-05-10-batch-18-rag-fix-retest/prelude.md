# Batch 18 — RAG Phase 1 Fix Retest Prelude

> Batch 17 で surface した 6 件 release-blocker の fix wave (= 5 commits、
> `0014310..fa05e8c`) + embedding 配線 fix (= `9681096`) 後の retest。
> 目的: 「production grade landed」 judgment を batch 14 水準復帰判定で
> 取り直す。 mini-retest 構成 (= S5/S6/S8/S9 のみ N=3 each、 total ~12 runs)。

## 1. Batch 18 直前の Reyn 状態

main HEAD `9681096`、 2223 passed / 2 xfailed (= replay fixture 4 件再録音含む)。
Fix wave 着地内訳:

| Bug ID | Severity | Fix commit | 影響 scenario |
|---|---|---|---|
| B17-S6-1 / S8-2 | CRITICAL | `0014310` (= recall + drop_source を build_tools + dispatch frozenset に追加) | S5 / S6 / S8 |
| B17-S9-1 | CRITICAL | `a4c1b47` (= abort CandidateOutput を _build_candidates に追加) | S9 |
| B17-S1-1 / S5-3 | HIGH | `2d3e531` (= router prompt vocab disambiguation + empty state hint) | S5 |
| B17-S5-2 / S8-1 | HIGH | `d670839` (= SourceManifest mtime poll で cross-process cache invalidate) | S5 / S6 / S8 |
| B17-S8-3 | HIGH | `fa05e8c` (= router op context PermissionDecl に index_drop=True 宣言) | S8 |
| 配線 fix | (post) | `9681096` (= EmbeddingConfig dataclass + dict response 両 accept) | 全 indexing 経路 |

## 2. Batch 18 のゴール

1. **S5 (= headline)**: recall tool invoke rate を 0/5 (= batch 17) → **3/3 (= 100%)** に復帰
2. **S6**: multi-source recall で sources field に 2 source 含むこと (= batch 17 は build_tools 漏れで全 0/3)
3. **S8**: drop_source tool 経由の destructive op 経路が permission gate 経由で動作 (= batch 17 は permission decl 漏れで blocked)
4. **S9**: cost preflight で LLM が `decision: "abort"` 出力可能 (= batch 17 は abort candidate 不在で構造的 impossible)

batch 14 milestone 水準 (= verified rate 70%+) 復帰判定を最終 verdict に。

## 3. Out of scope

- B17-S5-1 (= `<ctrl42>` code-hallucination): gemini-2.5-flash-lite quirk、 phase 2 model selection wave で対応
- B17-S3-2 / S9-2 / S10-1 / S7-1/S7-2 / S10-2: MED/LOW deferred、 batch 18 後の sweep wave で
- Phase 1.5 memory migration: 別 wave、 1.0 release は memory inline 不変で landed 済

## 4. Embedding 経路

batch 17 は OPENAI_API_KEY 不在で `FakeEmbeddingProvider` 経由だった。 Batch 18
直前に LiteLLM proxy 経由 `gemini-embedding-001` (= `text-embedding-3-small/large`
alias) の wiring が landed。 ただし fix wave 5 件は **router / dispatch / permission
layer** にのみ着地しており、 embedding layer は無関係。 retest 公平性のため
**FakeEmbeddingProvider 路線を継続** (= batch 17 と同 provider、 LLM-visible
配線 only の effect 検証に focus)。 real embedding path は phase 1.5 dogfood 担当。

## 5. 4 シナリオ + 予測

各 scenario は独立 worktree + 独立 `.reyn/` state、 sonnet sub-agent が driver。
N=3 が default、 critical scenario (= S5) のみ N=3 据え置き (= mini-retest なので
batch 17 N=5 から減らす)。 total ~12 runs。

### S5: Recall via chat (= headline scenario)

**Prompt**: 「What does the recall tool do? Search the docs.」 (= batch 17 と同)
**期待**: LLM が `recall(query=..., sources=["reyn_docs"], top_k=5)` invoke、 result chunks が reply に組込み
**Sample**: N=3
**予測**: verified **80%** / refuted 15% (= R-RAG1 attractor 残存) / inconclusive 5% / blocked 0%

> 根拠: build_tools 漏れ fix で tool が LLM 視界に出現、 vocab disambiguation で
> "Memory access" intent と区別できる。 残 attractor は B17-S5-1 ctrl42 (= 別系統、
> 1/3 程度発生想定)。

### S6: Multi-source recall

**Prompt**: 「How is recall implemented?」 (= batch 17 と同)、 reyn_docs + reyn_src 2 source seed
**期待**: tool_call args の sources field に 2 source 含む (= 順序問わず)
**Sample**: N=3
**予測**: verified **70%** / refuted 25% (= LLM が 1 source で満足) / inconclusive 5% / blocked 0%

### S8: drop_source via chat + permission ask

**Prompt**: 「Remove the test_drop source from the index」 (= batch 17 と同)
**期待**: drop_source tool invoke + permission ask 発火 + user `y` 入力で SQLite ファイル消滅
**Sample**: N=3
**予測**: verified **75%** / refuted 20% (= LLM が text-reply で CLI 案内) / inconclusive 5% / blocked 0%

### S9: Cost preflight gate

**Prompt**: `reyn run index_docs --source large --path "src/reyn/**/*.py"` + cost_warn_threshold=5
**期待**: Phase 1 LLM が `control.type: "abort"` 出力 (= abort candidate 利用可能)
**Sample**: N=3
**予測**: verified **70%** / refuted 25% (= LLM が threshold を ignore) / inconclusive 5% / blocked 0%

## 6. Aggregate prediction summary

| 項目 | 予測 |
|---|---|
| total runs | 12 (= 4 scenarios × N=3) |
| mean verified rate | ~74% (= 80+70+75+70 / 4) |
| Brier (scenario 平均) | ~0.10 想定 (= batch 14 水準復帰、 batch 17 = 0.32 から大幅改善見込み) |
| 新 bug count | 0-2 (= fix wave で主要経路 close、 残 attractor は known) |

batch 14 milestone (= verified 70%+) 復帰判定を **mean verified ≥ 70%** で確定する。

## 7. R-attractor 候補 (= batch 17 経験ベース)

| ID | Description | 候補 scenario |
|---|---|---|
| R-RAG1 | recall invoke 忘れ → memory tool 走り | S5 / S6 |
| R-RAG-ctrl42 | gemini ctrl42 code-hallucination | S5 / S6 (= 1/3 程度残存想定) |
| R-RAG-textreply | LLM が tool invoke せず CLI 案内 text | S8 |
| R-RAG-ignore-threshold | cost preflight 結果 ignore で strategy 出力 | S9 |

## 8. 並列実行構成

4 sonnet sub-agents、 worktree isolation、 各 agent が 1 scenario 担当 (= S5/S6/S8/S9)。
user 制限により sonnet 最大並列は 6 (= 4 < 6 で safe)。

## 9. Calibration discipline

batch 17 retrospective で確立した **新原則 10 (= structural pre-check)** を
operationalize: prediction は 「LLM 視界 + OS dispatch 経路に存在する」 前提で
立てる (= fix wave で全 4 scenario の structural pre-check 済)。 観測は LLM
attractor のみに focus。

「production grade landed」 narrative を再構築する batch。 verified rate ≥ 70% 復帰で
batch 14 milestone parity と判定、 < 70% なら追加 fix wave (= 残 attractor 観測 +
原因特定 + fix dispatch) を発動する。
