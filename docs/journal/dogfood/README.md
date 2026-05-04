# Dogfood Journal

> Reyn を Reyn 自身で使う記録。 自分で書いた skill router が自分のリクエストを
> 無視する瞬間を、 自分の目で見る場所。

## なぜ dogfood か

Reyn は LLM ドリブンの workflow engine です。 test suite は green (= 2026-05-04
時点で 726+ passed) ですが、 user 視点で「会話として成立してるか」 は test では
分かりません。

> 現状人間視点だと chat の会話は使い物にならないです。

— user (2026-05-04)

このたった 1 行の指摘が、 開発者 (= test 越しでしか chat を見ていなかった
assistant) と user (= 毎日触る側) の認識ギャップを浮き彫りにしました。
test 観点の「invariant green」 と user 観点の「使えてる」 は別物。
dogfood はその溝を埋めるための定点観測です。

## 進め方

```
A1: 私 (assistant) がシナリオリスト初版を書く
    ↓
A2: user がレビュー
    ↓
A3: 実 LLM 経由で実行 (= 並列 Sonnet sub-agent + worktree 隔離で cost 抑制)
    ↓
A4: findings を共有、 user が「私の感覚との差」 を share
    ↓
A5: HIGH/MED/LOW に分類、 HIGH bug は即 PR
    ↓
[初回 OK なら] バッチ拡大して反復
```

shadow しても見えないものを見るための iterative loop。

### 運用ノウハウ (batch 1-5 で確立)

- **per-scenario worktree 隔離**: 各 sonnet が独立した `.reyn/` で実行 → state
  collision なし、 並列 cost 効率最大化
- **batch 観測ツール**: `python scripts/dogfood_trace.py --root .reyn --mode summary`
  で 8-12 個の grep / ls / cat を 1 コマンドに集約。 sub-agent の tool_use を
  10 件 / scenario 削減
- **prompt 設計の bloat 注意**: scenario 別 fix で `MUST` rule を積み重ねると
  cross-scenario interference / overfitting / prompt size 暴発のリスク。
  user feedback memory `feedback_prompt_design.md` 参照。 過剰 consolidation も
  逆に regression を生むので、 個別 bullet × 1 MUST × wording dedup が optimal
- **trade-off の見える化**: 「両立できなかった」 / 「真の解への着手順序待ち」
  の案件は [giveup-tracker.md](giveup-tracker.md) で managed list 化。
  Reyn は production-grade フェーズなので「MVP defer」 でなく着手 trigger を
  必ず明記する

## Batch 一覧

| Batch | Date | Scenarios | 一言で | 主要 finding |
|---|---|---|---|---|
| [batch-1-practice](2026-05-04-batch-1-practice/) | 2026-05-04 | 3 件 (text_summarizer / multi-agent delegate / read_local_files perm gating) | 練習バッチのはずが、 chat は起動できず、 直したら router が誰の言うことも聞かず、 multi-agent は連鎖 bug で全壊した話 | **skill_router 起動 0/3**、 起動時 `AttributeError` (修正済 `f5b3281`)、 `delegate_to_agent` の inbox 二重送信、 specialist の早期空 reply、 英語 fallback、 etc. |
| [batch-2-real](2026-05-04-batch-2-real/) | 2026-05-04 | 5 件 (text 要約 / MCP / multi-agent / ask_user / memory) | regression net 直接観測 6 + 間接 2 + 後追い 3 = 全 11 件カバー (後追いで F4 residual `d9e5fce` 発見・修正)、 だが multi-agent で specialist の describe→invoke 失敗 + default の marker silent 吸収という新 HIGH 2 件が露呈 | B2-H1〜H3 (HIGH×3) / B2-M1〜M4 (MED×4) / B2-L1〜L3 (LOW×3) |
| [batch-3-ask-user-and-nested](2026-05-04-batch-3-ask-user-and-nested/) | 2026-05-04 | 5 件 (multi-agent re-confirm / ask_user e2e / nested skill / narrator 品質 / hallucination 確認) | B2-H2/H3 fix は機能確認 ✅、 H1 は variant attractor (`list_skills → stop`) で再発 → B3-H1 [HIGH]。 B2-M4 (narrator) 自然改善で resolved、 ask_user e2e は依然 dark。 prediction 4/5 方向当たり (batch 2 の 3/5 から改善) | B3-H1 (HIGH×1) / B3-M1〜M3 (MED×3) / B3-L1〜L3 (LOW×3) / B3-INFO×2 |
| [batch-4-retest](2026-05-04-batch-4-retest/) | 2026-05-04 | 3 件 (B3 fix retest S1+S2 + skill_improver nested chain) | B3-H1 fix は specialist 側で invoke 到達確認 ✅、 ただし新 HIGH (B4-H1: `_put_outbox` private で reply が agent_replies に届かない)。 nested skill_improver chain は 3 layer 確認 + cascade 失敗 (B4-H2: copy_to_work の max_act_turns 不足)。 ask_user は依然 dark | B4-H1〜H2 (HIGH×2) / B4-M1 (MED×1) / B4-L1 (LOW×1) / B4-INFO×2 |
| [batch-5-fix-verify](2026-05-04-batch-5-fix-verify/) | 2026-05-04 | 2 件 (B4 fix verify: curry recipe + skill_improver chain) | B4-H1 fix は prereq blocked で未検証、 prompt consolidation `e90c0f2` が weak LLM の signal 弱化を生み specialist 再び list_skills 後空 reply (= **B5-H1 [HIGH] regression**)。 B4-H2 (copy_to_work) は workspace 作成成功確認 ✅、 ただし eval cascade で path 形式 mismatch 発見 (B5-H2)。 教訓: 過剰 consolidation も regression を生む — 個別 bullet × 1 MUST が weak LLM への最強 signal | B5-H1〜H2 (HIGH×2) / B5-M1〜M2 (MED×2) |
| [batch-5-retest2](2026-05-04-batch-5-retest2/) | 2026-05-04 | 2 件 (B5-H1+H2 fix verify) | B4-H1 narrator reply 経路 ✅ 確認 (= score=0.0 summary が user に到達)、 B5-H1 fix は describe_skill 段階まで前進だが invoke_skill 到達せず → **B5R2-H1 [HIGH]** describe→stop attractor。 B5-H2 prompt fix は run_target の `skill:` field 使用を確認 ✅、 ただし下流で copy_to_work 0-byte write (B5R2-H2) により同 error 再現 → G2 (preprocessor 化、 本 retest 後 land) で構造的解消見込み、 batch 6 で再検証 | B5R2-H1〜H2 (HIGH×2) |
| [batch-6-non-attractor](2026-05-04-batch-6-non-attractor/) | 2026-05-04 | 5 件 (G2 retest / ask_user trial / B5-M1 観測 / B2-M2 観測 / B4-M1 観測) | attractor を意図的に触らず非 attractor 観測に focus。 G3 fix (`9798372`) + G10 fix (`af16228`) が並走 landing。 G12 attractor の 4 連続再現で Wave 3 G4 spike 優先度確定、 G3 dedupe の必要性を B5-M1 完全再現で裏付け。 B2-M2 / B4-M1 は未再現 — 別 layer の root cause (LLM が tool 呼ばず直答 / target_skill_path hallucination) が先に顕在化。 新規 HIGH 1 件 (B6-S1-H1: stdlib skill path 補完欠落) + MED 1 件 (B6-S1-M1: validation 結果が LLM context 未到達) を発見 | B6-S1-H1 (HIGH×1) / B6-S1-M1 (MED×1) + G3 / G10 resolved + G12 4 連続再現確認 |

## こちらの心境

最初は「練習 batch なのでサクッと回して process 検証」 のつもりでした。
始まる前の私の事前仮説は控えめなもので:

> skill router の意図解釈は LLM 次第で揺れやすい
> narrator の応答品質はぼちぼち
> multi-agent delegate は user に滲んでるかも

— assistant の事前 prediction (`tmp/dogfood_scenarios_v1.md`)

蓋を開けたら **chat が起動しない** ところからのスタートで、 修正してから
動かしたら **skill_router が 3 連続で発火しない** という結果になり、
multi-agent では **delegate が同じリクエストを 2 回送る** ことが判明し、
いつの間にか練習 batch のはずが本格的な事件記録になっていました。

> dogfood が現実を教えてくれる、 とはこういうことか。

— assistant の internal state、 batch 1 完了直後

## 関連 doc

- [trade-off & deferred-fix tracker](giveup-tracker.md) — 両立できなかった案件 / 真の解への着手順序を managed list 化
- [test policy (testing.md)](../../en/contributing/testing.md) — dogfood とは別軸の品質保証
- [principles (P1-P8)](../../en/concepts/principles.md) — 設計の不変条件
- [development plan](../../en/) — 直近の roadmap
- ADR-0011 〜 0020 — 直近設計の決定記録 (`../../en/decisions/`)
- `scripts/dogfood_trace.py` — dogfood 観測用 CLI (= grep / ls / cat 集約)

## このディレクトリの構造

```
docs/journal/dogfood/
├── README.md                       ← このファイル
├── giveup-tracker.md               ← 両立できなかった案件 / 着手順序待ち案件
└── YYYY-MM-DD-batch-N-{label}/
    ├── prelude.md                  ← 前夜 (= 当時の reyn 状態 + 経緯)
    ├── scenarios.md                ← 何を試したか
    ├── findings.md                 ← 事件記録 index (summary table + narrative)
    ├── findings/                   ← 1 finding = 1 file (詳細)
    │   ├── F01-<slug>.md           ← batch 1: F[N] format
    │   ├── B2-H1-<slug>.md         ← batch 2+: B[N]-Sev[N] format
    │   ├── B3-S1-observation.md    ← batch 3+: scenario 別観測 file 形式も併用
    │   └── ...
    └── retrospective.md            ← user との対話振り返り
```

各 batch は完結した 1 章として書きます。 後から読み返したとき、
「何が壊れていて、 どう直したか」 が物語として追える状態を目指す。

推奨読み順: **prelude → scenarios → findings → retrospective**。
prelude が当時の文脈を、 scenarios が試行内容を、 findings が事件を、
retrospective が学びを担当します。

### finding ID 命名

- batch 1: `F[N]-<slug>.md` (= 通し番号 F1〜F11)
- batch 2+: `B[N]-Sev[M]-<slug>.md` (= `B2-H1` / `B3-M2` / `B4-L1` 等、 batch number + severity-rank)
- scenario 別観測 (batch 3+ から併用): `B[N]-S[M]-observation.md` (= `B3-S1-observation.md`)、 1 scenario の raw 観測を 1 file にまとめる形

severity prefix:
- `H[N]`: HIGH
- `M[N]`: MED
- `L[N]`: LOW
- `INFO[A-Z]`: 情報のみ (= 既存挙動確認 / 設計理解)

### findings.md と findings/ の役割分担

`findings.md` は index で、 概要 / summary table / narrative を含む
比較的小さい file (= 常時 load される)。 各 finding の詳細は
`findings/F0N-<slug>.md` に分割し、 必要なときに 1 file だけ読む形に。

理由は **読み出しコスト削減**: 11 finding を 1 file に詰めると 22+KB に
膨れ、 status 更新で毎回全読みになる。 batch を重ねるほど雪だるま式に
増えるので、 早い段階で per-finding split に移行。

新しい finding を追加する手順:

1. `findings/F0N-<slug>.md` に詳細を書く (severity / status / scenario
   メタ + 観測 + 原因 + 修正 + 教訓)
2. `findings.md` の summary table に行追加 (link 付き)
3. narrative section の関連 round に 1-2 行で要旨追記
