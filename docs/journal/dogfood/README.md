# Dogfood Journal

> Reyn を Reyn 自身で使う記録。 自分で書いた skill router が自分のリクエストを
> 無視する瞬間を、 自分の目で見る場所。

## なぜ dogfood か

Reyn は LLM ドリブンの workflow engine です。 test suite は green (今 642 passed)
ですが、 user 視点で「会話として成立してるか」 は test では分かりません。

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
A3: 私が実 LLM 経由で実行 (cost 削減のため Sonnet sub-agent に委託)
    ↓
A4: findings を共有、 user が「私の感覚との差」 を share
    ↓
A5: HIGH/MED/LOW に分類、 HIGH bug は即 PR
    ↓
[初回 OK なら] バッチ拡大して反復
```

shadow しても見えないものを見るための iterative loop。

## Batch 一覧

| Batch | Date | Scenarios | 一言で | 主要 finding |
|---|---|---|---|---|
| [batch-1-practice](2026-05-04-batch-1-practice/) | 2026-05-04 | 3 件 (text_summarizer / multi-agent delegate / read_local_files perm gating) | 練習バッチのはずが、 chat は起動できず、 直したら router が誰の言うことも聞かず、 multi-agent は連鎖 bug で全壊した話 | **skill_router 起動 0/3**、 起動時 `AttributeError` (修正済 `f5b3281`)、 `delegate_to_agent` の inbox 二重送信、 specialist の早期空 reply、 英語 fallback、 etc. |
| [batch-2-real](2026-05-04-batch-2-real/) | 2026-05-04 | 5 件 (text 要約 / MCP / multi-agent / ask_user / memory) | regression net 直接観測 6 + 間接 2 + 後追い 3 = 全 11 件カバー (後追いで F4 residual `d9e5fce` 発見・修正)、 だが multi-agent で specialist の describe→invoke 失敗 + default の marker silent 吸収という新 HIGH 2 件が露呈 | B2-H1〜H3 (HIGH×3) / B2-M1〜M4 (MED×4) / B2-L1〜L3 (LOW×3) |
| [batch-3-ask-user-and-nested](2026-05-04-batch-3-ask-user-and-nested/) | 2026-05-04 | 5 件 (multi-agent re-confirm / ask_user e2e / nested skill / narrator 品質 / hallucination 確認) | batch 2 HIGH 3 件 fix 後の e2e 再確認 + ask_user IR op / nested skill (run_skill) 初観測。 B2-INFO 再設計の実 LLM 検証 | TBD |

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

- [test policy (testing.md)](../../en/contributing/testing.md) — dogfood とは別軸の品質保証
- [principles (P1-P8)](../../en/concepts/principles.md) — 設計の不変条件
- [development plan](../../en/) — 直近の roadmap
- ADR-0011 〜 0020 — 直近設計の決定記録 (`../../en/decisions/`)

## このディレクトリの構造

```
docs/journal/dogfood/
├── README.md                       ← このファイル
└── YYYY-MM-DD-batch-N-{label}/
    ├── prelude.md                  ← 前夜 (= 当時の reyn 状態 + 経緯)
    ├── scenarios.md                ← 何を試したか
    ├── findings.md                 ← 事件記録 index (summary table + narrative)
    ├── findings/                   ← 1 finding = 1 file (詳細)
    │   ├── F01-<slug>.md
    │   ├── F02-<slug>.md
    │   └── ...
    └── retrospective.md            ← user との対話振り返り
```

各 batch は完結した 1 章として書きます。 後から読み返したとき、
「何が壊れていて、 どう直したか」 が物語として追える状態を目指す。

推奨読み順: **prelude → scenarios → findings → retrospective**。
prelude が当時の文脈を、 scenarios が試行内容を、 findings が事件を、
retrospective が学びを担当します。

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
