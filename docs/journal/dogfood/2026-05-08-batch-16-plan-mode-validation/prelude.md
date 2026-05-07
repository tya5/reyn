# Batch 16 (plan-mode validation — first real LLM dogfood) — Prelude

> Phase 1 完了 (batch 14、 2026-05-06) 以降に landing した plan-mode 全実装
> (ADR-0022/0023/0024/0025 + Phase 2.1 async dispatch) を **初めて実 LLM で観測**
> する batch。 30+ commit が Tier 2 test で validated だが real LLM 未通過。
> 設計の成立 / 不成立を observe-first で記録する。

---

## 1. Batch 16 直前の Reyn 状態

| 項目 | 値 |
|---|---|
| Date | 2026-05-08 |
| main HEAD | `4912457` |
| Test suite | 1373 passed / 2 xfailed (= ADR-0023 Phase 2 で 94 new Tier 2 tests 追加) |
| 最終 real LLM batch | batch 14 (2026-05-06、 N=5 chain 100%、 production-grade phase 1 完了) |
| Plan-mode commits | 30+ (= batch 14 以降、 全て Tier 2 only、 real LLM 未通過) |

### Plan-mode landing 履歴 (新着順)

```
4912457  test(plan): multi-plan crash + resume integration (Tier 2, ADR-0023 §2.1.5)
4cc764c  feat(plan): ADR-0025 sub-loop LLM memo + Phase 2 fresh-run wiring fix
80e4977  feat(plan): ADR-0024 per-plan step result spill-to-file (R-D10 mirror)
619b2ad  feat(plan): /plan resume --from <step_id> + slash docs (ADR-0023 §3.7)
891f0fd  docs(plan-mode): concept docs (en + ja) + plan file completion entries
b1da83b  feat(plan): WAL truncation floor + /plan slash commands (ADR-0023 follow-up)
a13caaa  feat(plan): Phase 2.1 — async dispatch + per-plan chain_id (ADR-0023)
a03e69f  docs(adr): ADR-0023 Phase 2.1 amendment — async dispatch + multi-plan
3a7c02f  docs(adr): ADR-0023 status Draft → Accepted + Implemented
1e529d7  feat(plan): step 7d — ChatSession + AgentRegistry auto-resume (ADR-0023 Phase 2)
5279341  feat(plan): step 7c — PlanResumeCoordinator + reyn.yaml policy (ADR-0023 Phase 2)
f1d81e3  feat(plan): step 7a/7b — PlanResumeAnalyzer + memo replay (ADR-0023 Phase 2)
c58840e  feat(plan): step 6 — dispatch_plan_tool via PlanRuntime + artifact (ADR-0023 Phase 2)
...
```

### Batch 1–15 の軌跡 (参照)

| Batch | Date | マイルストーン |
|---|---|---|
| 1–6 | 2026-05-04 | 基盤 fix / attractor 特定 |
| 7–9 | 2026-05-04 | infra 整備 + fix wave |
| 10 | 2026-05-05 | 「chain 完走史上初」 (後に N=1 lucky case と判明) |
| 11 | 2026-05-05 | N=5 測定、 B11-NEW-1 blocker 特定 |
| 12 | 2026-05-06 | N=5 real milestone 確定 (3/5+) |
| 13 | 2026-05-06 | doc 違反 revert + V3 wording + N=5 (4/5 = 80%) |
| 14 | 2026-05-06 | N=5 (5/5 = 100%) → production-grade phase 1 完了 |
| 15 | — | plan-mode 設計 ADR / spike (= real LLM 未実施) |

---

## 2. Batch 16 のゴール (= 観測したい問い)

plan-mode 実装が Tier 2 test で closed-form に検証された設計どおりに、
**実 LLM + 実 I/O** 下で動作するかを観測する。 問いを 5 つに絞る:

| # | 問い |
|---|---|
| G1 | Router LLM は multi-source / multi-step タスクに対して `plan` tool を自律的に invoke するか |
| G2 | Crash (`:cancel` / kill -9) 後の auto-resume で memo replay が正しく fire するか |
| G3 | 32 KB を超える step result が spill-to-file を自動 trigger するか |
| G4 | 並行 plan 2 本が `/plan list` で正しく visible になり、 両方の terminal text が user に届くか |
| G5 | `/plan list / discard / resume --from` operator コマンドが real session で period どおりに機能するか |

---

## 3. Out of scope

以下は batch 16 で観測しない:

- **Sub-loop tool-op memoization** (= ADR-0023 §3.4 明示 defer、 LLM-cost 観測 motivation のみ記録)
- **Audit hash chain** (= scope 独立、 plan-mode と無関係)
- **OSS Lv.1 release prep** (= 別 axis)
- **G4 spike (strong model)** (= cost 10x deferred、 batch 14 判断継続)

---

## 4. 5 シナリオ

### S1: multi-source synthesis

**1-line goal**: router が 2 読み込み + 1 合成 の 3-step plan を自律的に立案するかを観測。

**ユーザー prompt 系列**:
```
> "README.md と CLAUDE.md を読んで、両者を比較する 3 段落の文章を書いて"
```

**利用可能 tool / skill**: `file_read`、 `direct_llm` (or plan tool 経由の sub-step)

**観測ポイント**:
- `plan` tool が invoke された (= events に `plan_created` が存在)
- step 数が 2 以上 (= 1 file_read + 1 synthesis 以上)
- 最終 aggregator の出力テキストが user の outbox に届く (= `kind="agent"` event)
- step ごとの `plan_step_completed` event が WAL に記録される
- Plan の全 step が complete になる (= `plan_completed` event)

**検証基準**:
- **verified**: `plan` invoke + 3-step 構成 + terminal text 到達
- **inconclusive**: plan invoke されたが step 数が 1 (= 合成のみ、 読み込みは plan 外)
- **refuted**: router が直接 text-reply (= plan invoke なし)
- **blocked**: `plan` tool 自体がクラッシュ / exception

**予測**:
- verified: 40%
- inconclusive: 20%
- refuted (plan invoke なし): 30%
- blocked: 10%

> **リスクノート**: batch 1-14 で router LLM の text-reply attractor (= G1/G23) を
> 繰り返し観測。 plan tool は新規追加なので attractor の degree は未知。 refuted 30%
> は保守的だが現実的。

---

### S2: concurrent plans

**1-line goal**: 2 本の plan が独立して走り、 両方の terminal text が user に届くかを観測。

**ユーザー prompt 系列**:
```
# turn 1
> "src/reyn/ 以下のファイル一覧を列挙して、各ファイルの役割を 1 行で説明して"
# turn 2 (turn 1 の plan が完了する前に送信)
> "CLAUDE.md を読んで、最も重要なルールを 3 つ挙げて"
```

**利用可能 tool / skill**: `file_read`、 `list_files`、 `direct_llm`

**観測ポイント**:
- `/plan list` で 2 本が表示される (= 2 個の `plan_id` が active)
- `plan_created` event が 2 件
- 両方の plan に `plan_completed` event が来る
- terminal text が completion order で 2 件 outbox に届く (= UI ordering)
- `plan_step_completed` が plan_id ごとに正しく分離されている

**検証基準**:
- **verified**: 2 plan 同時 active + 両 terminal 到達 + `/plan list` 正常表示
- **inconclusive**: 1 本は完了、 もう 1 本が途中で詰まる
- **refuted**: 2 本目 plan が 1 本目完了を wait して直列化 (= async 実装バグ)
- **blocked**: 2 本目 turn で exception / crash

**予測**:
- verified: 35%
- inconclusive: 25%
- refuted (直列化): 20%
- blocked: 20%

> **リスクノート**: Phase 2.1 async dispatch は Tier 2 で multi-plan を検証済み (= `4912457`)
> だが、 real RouterLoop と ChatSession の組み合わせは未観測。 outbox ordering が
> TUI 側に正しく伝わるかも未確認。

---

### S3: crash + resume

**1-line goal**: plan 実行中に crash させ、 auto-resume + memo replay が火を噴くかを観測。

**ユーザー prompt 系列**:
```
# turn 1: 4-5 step の plan を trigger
> "src/reyn/ 以下の各 Python ファイルを読んで、クラス名と主要メソッドを列挙して"
# step 2 or 3 の途中で:
#   option A: ":cancel" を送信
#   option B: process に SIGKILL (= kill -9 <pid>)
# その後 reyn chat を再起動
```

**利用可能 tool / skill**: `file_read`、 `list_files`

**観測ポイント**:
- 再起動後に auto-resume が fire する (= `plan_resume_started` event)
- `PlanResumeAnalyzer` が completed step を `s1..sk` と判定 (= events に記録)
- crashed step が re-execute される (= `plan_step_started` が再度 emit)
- completed step の LLM memo が replay される (= fresh LLM call なし、 cost 節約)
- `dogfood_trace.py --mode plan-snapshot <plan_id>` で snapshot が健全

**検証基準**:
- **verified**: auto-resume + crashed step 以降が re-execute + completed step は skip
- **inconclusive**: auto-resume は fire したが memo replay が不完全 (= 一部 LLM 再実行)
- **refuted**: auto-resume しない (= plan が `abandoned` のまま)
- **blocked**: resume 中に exception / crash (= 二重 crash)

**予測**:
- verified: 45%
- inconclusive: 25%
- refuted: 15%
- blocked: 15%

> **リスクノート**: `:cancel` と kill -9 では crash context が異なる (= WAL flush 済みか否か)。
> option A (`:cancel`) は WAL truncation floor (= `b1da83b`) が関係するので先に試す。
> ADR-0025 の LLM memo replay は `4cc764c` で実装済みだが fresh-run wiring fix も同 commit
> なので両方を同時に観測することになる。

---

### S4: operator commands

**1-line goal**: `/plan list`、 `/plan discard <plan_id>`、 `/plan resume <plan_id> --from <step_id>` が real session で設計どおりに動くかを観測。

**ユーザー prompt 系列**:
```
# turn 1: plan を起動
> "README.md と CLAUDE.md と docs/en/concepts/plan-mode.md を読んで要約して"
# step 1 完了直後 (= まだ plan running 中) に:
> "/plan list"
# plan 完了後:
> "/plan discard <plan_id>"
# 別途: step 2 完了時点で resume をテスト
> "/plan resume <plan_id> --from <step_id>"
```

**利用可能 tool / skill**: `file_read`

**観測ポイント**:
- `/plan list` が plan_id + status + step 進捗を表示する
- `/plan discard <plan_id>` が plan を discarded state にし、 以後の step が中止される
- discard 後に cross-agent notify (= `notify_chain_discarded`) が fire する
- `/plan resume --from <step_id>` が指定 step から replay を開始する
- cleanup 後に `plan_discarded` event が WAL に記録される

**検証基準**:
- **verified**: 3 コマンド全てが設計どおりの state 遷移 + event を生成
- **inconclusive**: 一部コマンドが no-op または部分的に機能
- **refuted**: コマンドが認識されない / slash handler が未登録
- **blocked**: いずれかのコマンドで exception

**予測**:
- verified: 50%
- inconclusive: 25%
- refuted: 10%
- blocked: 15%

> **リスクノート**: slash コマンド登録は `b1da83b` + `619b2ad` で実装済み。
> ただし slash handler と RouterLoop / PlanRegistry の integration は Tier 2 test の
> カバレッジ範囲外の可能性あり。 real session での挙動は未知。

---

### S5: large output (32 KB+ spill)

**1-line goal**: 1 step の出力が 32 KB を超えたときに spill-to-file が自動 trigger するかを観測。

**ユーザー prompt 系列**:
```
> "src/reyn/ 以下の全 Python ファイルを列挙し、各ファイルについて
>  クラス名・主要メソッド・役割を 2-3 文で説明して"
```

**利用可能 tool / skill**: `file_read`、 `list_files`、 `direct_llm`

**観測ポイント**:
- step result の文字数が 32,768 超 (= `STEP_RESULT_MAX_CHARS` threshold)
- `.reyn/plans/<plan_id>/step_<sid>.txt` ファイルが生成される (= spill-to-file)
- `PlanSnapshot.step_results[sid]` が `{"ref": "step_<sid>.txt"}` 形式になる
- downstream synthesis step が spill ファイルから full text を読み込む
- `dogfood_trace.py --mode plan-snapshot <plan_id>` で ref 形式を確認できる
- `[truncated]` suffix が **ない** (= lossy truncation の排除を確認)

**検証基準**:
- **verified**: spill file 生成 + downstream step が full text を受信 + truncation なし
- **inconclusive**: spill は trigger したが downstream step が ref 解決に失敗 (= partial text)
- **refuted**: 32 KB 超でも inline 保存 (= spill 未発火 / threshold 設定ミス)
- **blocked**: spill 書き込みで I/O exception

**予測**:
- verified: 55%
- inconclusive: 20%
- refuted: 15%
- blocked: 10%

> **リスクノート**: ADR-0024 spill は `80e4977` で実装、 Tier 2 で threshold 動作を確認済み。
> ただし「src/reyn/ 全ファイル記述」が実際に 32 KB を超えるかは LLM の verbosity 依存。
> 超えない場合は別プロンプト (= 大きな log ファイルの解析等) でリトライ。

---

## 5. N=5 信頼性目標

| 指標 | 目標 |
|---|---|
| 実行回数 | 各シナリオ × N=5 (= 計 25 runs) |
| production-grade pass 基準 | 各シナリオ 4/5 以上 verified |
| Brier score 目標 | ≤ 0.30 (= batch 13-14 の 0.20 水準を意識しつつ、 初回 plan-mode で uncertainty 高め) |
| 全体 pass 基準 | 5 シナリオ中 3 以上が 4/5 verified (= batch 14 と同じ閾値) |

Brier score 入力:

| シナリオ | verified 予測 | refuted 予測 | blocked 予測 |
|---|---|---|---|
| S1 (synthesis) | 40% | 30% | 10% |
| S2 (concurrent) | 35% | 20% | 20% |
| S3 (crash+resume) | 45% | 15% | 15% |
| S4 (operator cmd) | 50% | 10% | 15% |
| S5 (32KB spill) | 55% | 15% | 10% |

---

## 6. 実行フロー (A1–A5 チェックポイント)

```
A1: このプレリュード (= シナリオ設計完了)
    ↓
A2: ユーザーがシナリオを review + 承認 (= A3 dispatch 前の必須 gate)
    ↓
A3: 並列 Sonnet dispatch (= シナリオごとに別 worktree + 別 agent_state_dir)
    5 session × N=5 = 25 runs
    各 session: REYN_LLM_TRACE_DUMP=/tmp/batch16/<scenario>.jsonl
    ↓
A4: 各 session の findings を aggregate
    dogfood_trace.py --mode plan-summary --root <session_root>
    → CRITICAL / HIGH / MED / LOW に分類
    ↓
A5: fix wave または tracker へ登録
    ↓
retrospective: prediction calibration + Brier score + open issues
```

### A3 並列 dispatch の隔離方針 (= 実装版、 2026-05-08 改訂)

**A2A JSON-RPC over HTTP driver** を採用 (= past hn_dogfood_v8.py pattern)。
pexpect 経由 `reyn chat` よりも cleaner、 既稼働の web server を再利用。

各 scenario sonnet session:
- **専用 agent**: `b16_s<N>` (= `.reyn/agents/b16_s<N>/profile.yaml` 直書きで作成済)
- **driver**: `/tmp/batch16/driver.py` (= 共通 helpers: `send_message`,
  `clean_state`, `snapshot_state`, `plan_summary`, `run_scenario`)
- **endpoint**: `http://localhost:8080/a2a/agents/b16_s<N>` (= existing web)
- **state isolation**: agent 名で隔離、 各 run 前に `clean_state()` で
  `.reyn/agents/b16_s<N>/state/` + `events/` を rmtree
- **per-run snapshot**: `/tmp/batch16/<scenario_id>/run_<i>/` に
  `agent/` + `wal.jsonl` を copy 保存

→ session 間の干渉源は **共有 web server process** だが、 agent state は完全
独立。 これは production multi-agent と同じ shape なので、 干渉自体が
data (= S2 concurrent plans の予行)。

注意: `--agent-state-dir` flag は `reyn chat` に **存在しない** (= prelude 初版の
仮定誤り)。 agent_name positional + デフォルト `.reyn/` が production 設計。

---

## 7. Tooling リマインダー

| ツール | 用途 |
|---|---|
| `REYN_LLM_TRACE_DUMP=/tmp/batch16/<scenario>.jsonl` | 各 session の LLM call 全記録 |
| `dogfood_trace.py --mode plan-summary --root <session_root>` | plan 実行サマリ (step 進捗 + status) |
| `dogfood_trace.py --mode plan-trace <plan_id>` | 異常 plan の step-by-step trace |
| `dogfood_trace.py --mode plan-snapshot <plan_id>` | crash recovery シナリオの state 検査 |
| `dogfood_trace.py --mode cost` | memo 節約が cost summary に反映されているか確認 |
| `dogfood_trace.py --mode summary --root <session_root>` | event 全体サマリ (= 既存 mode、 比較用) |

---

## 8. Open questions / リスク項目

| # | リスク | 観測 signal |
|---|---|---|
| R1 | Router LLM が `plan` tool を invoke しない (= G1/G23 attractor の plan-mode 版) | S1 / S2 で refuted rate > 40% |
| R2 | async dispatch の outbox ordering が TUI に正しく届かない (= multi-plan UX confusion) | S2 で terminal text の ordering が逆転 / 欠落 |
| R3 | 32 KB threshold が weak LLM の実際の verbosity に対して高すぎる (= spill 未発火) | S5 で actual output < 32 KB |
| R4 | WAL truncation floor と `:cancel` の interaction で resume が不完全 | S3 で inconclusive rate > 30% |
| R5 | `/plan resume --from` が step_id の形式不一致で拒否 (= slash handler の input validation) | S4 で blocked rate > 20% |
| R6 | multi-plan 時に plan_id が TUI に表示されず operator が `/plan discard` できない | S4 で list 表示が空 |

---

## 9. 参照リンク

- batch 14 retro: `../2026-05-06-batch-14-stability-extension/retrospective.md`
- ADR-0022 (crash fail-safe): `../../en/decisions/0022-plan-mode-crash-fail-safe.md`
- ADR-0023 (forward replay + Phase 2.1 async): `../../en/decisions/0023-plan-mode-forward-replay.md`
- ADR-0024 (step result spill): `../../en/decisions/0024-plan-step-result-spill.md`
- ADR-0025 (sub-loop LLM memo): `../../en/decisions/0025-plan-step-llm-memoization.md`
- plan-mode concept (en): `../../en/concepts/plan-mode.md`
- plan-mode concept (ja): `../../ja/concepts/plan-mode.md`
- dogfood discipline: `../../en/contributing/dogfood-discipline.md`
- giveup-tracker: `../giveup-tracker.md`
