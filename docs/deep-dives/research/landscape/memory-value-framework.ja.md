---
title: "記憶の価値軸 — Behavioral Sensitivity 駆動の記憶管理"
last_updated: 2026-05-14
status: draft
---

# 記憶の価値軸 — Behavioral Sensitivity 駆動の記憶管理

> **「記憶とは何を保存するかではなく、何を信じるかの問題だ。」**

このドキュメントは **living research note** として、気長に深掘りしていく概念スケッチです。
FP（実装提案）ではなく、アイデアの育成場所として位置づけます。

---

## 動機 — なぜ「価値」で記憶を管理するか

### 既存アプローチの限界

現在の AI agent における記憶管理は、ほぼ例外なく **proxy metric** に依存している。

| アプローチ | 代理指標 | 問題 |
|---|---|---|
| 時間軸型（エピソード/意味記憶） | 経過時間 | 最近の記憶が必ずしも有用ではない |
| 頻度型（LRU / LFU 相当） | アクセス回数 | 重要だが稀な記憶が消える |
| 意味カテゴリ型 | タグ分類 | カテゴリ境界が恣意的 |
| ベクトル類似度型 | 埋め込み距離 | 「似ている」≠「役に立つ」 |

これらはすべて「記憶が有用かどうか」を間接的に推定しているに過ぎない。

### 記憶は「ストレージ最適化」ではなく「信念の更新」

記憶管理の本質的な問いは「容量をどう使うか」ではなく
**「今持っている信念が次の行動に影響を与えるか」** だ。

これを直接計測できるなら、proxy metric を経由する必要はない。

---

## 3 つの価値軸

### 1. Cache Value（再導出価値）

```
Cache Value = 再導出コスト − 保存コスト
```

- **正の場合**: 記憶として保持する価値あり（保存コスト < 再導出コスト）
- **負の場合**: 毎回再導出した方が安い（= 保持不要）
- **例**: `今日の天気` は再導出コストが低いため Cache Value は負になりやすい
- **例**: 「ユーザーのコーディングスタイル分析」は一度やれば高 Cache Value

この軸は主に **「何を記憶として格納するか」** の判断に使う。

### 2. Behavioral Sensitivity（行動感度）

```
Behavioral Sensitivity = |出力(記憶あり) − 出力(記憶なし)| / 保存コスト
```

- 記憶が **実際の意思決定にどれだけ影響を与えるか** を直接計測する
- **高い場合**: その記憶は現在の行動に効いている（保持価値大）
- **低い場合**: あってもなくても変わらない（候補に挙がってさえいない可能性）

#### Reyn との接続 — P6 イベントログが計測基盤になる

P6 イベントログには各フェーズの artifact と決定が記録されている。
`run_skill_started` → フェーズ遷移 → `final_output` の経路を比較すれば
「同一入力で記憶の有無が出力にどう影響したか」を事後的に計測できる。

```
計測フロー（仮）:
  セッション A: memory_loaded=True  → final_output: X
  セッション B: memory_loaded=False → final_output: Y
  Behavioral Sensitivity = |X − Y| / storage_cost(memory)
```

これは **既存のいかなる agent フレームワークも実装していない直接計測** であり、
Reyn 固有の差別化ポイントになりうる。

### 3. Abstraction Depth（抽象化深度）

```
エピソード（具体的出来事）
    ↓ 複数の共通パターン抽出
パターン（繰り返す構造）
    ↓ パターンの一般化
原則（普遍的な行動指針）
```

- **エピソード**: 「2026-05-14、ユーザーが X を好まなかった」
- **パターン**: 「ユーザーは出力の冗長さを嫌う傾向がある」
- **原則**: 「簡潔さを優先せよ」

Abstraction Depth が高いほど：
- 適用範囲が広い（汎化）
- 記憶の「密度」が高い（少ない保存量で多くの判断をカバー）
- 廃棄のタイミングを判断しやすい（「この原則は今も有効か？」と問える）

現在の Reyn の `feedback/` ディレクトリのエントリはこの軸で分類できる。

---

## 記憶を捨てる判断基準

「いつ忘れるか」は「いつ保存するか」と同じくらい重要な問い。

### 条件 1 — 矛盾する証拠が蓄積されたとき

記憶に反するアウトカムが一定数観測されたら、その記憶を更新 or 廃棄する。

```
例: 「ユーザーは箇条書きを好む」という記憶
    → 実際には散文を求めるフィードバックが 3 件連続した
    → 記憶を反転、または「コンテキスト依存」に格上げ（= Abstraction Depth 変化）
```

これは Behavioral Sensitivity で検知できる:
記憶ありの出力がユーザーに reject され続けるなら Sensitivity は「負の方向に高い」。

### 条件 2 — 関連する意思決定コンテキストが消滅したとき

記憶が有用だった前提条件がなくなった場合。

```
例: 「このプロジェクトは Python 3.9 を使う」という記憶
    → プロジェクトが Python 3.12 に移行した
    → 旧バージョン前提の記憶は Behavioral Sensitivity = 0 に近づく
```

Cache Value も下がる（再導出しても「Python 3.12」が返ってくるため）。

### 条件 3 — より一般的な信念に包含されたとき（Abstraction Depth の昇格）

エピソード → パターン → 原則 への昇格が起きたとき、
下位の記憶は冗長になる。

```
例:
  エピソード A: 「2026-05-10、ログ出力を省略したら怒られた」
  エピソード B: 「2026-05-12、省略したら確認を求められた」
  ↓ パターン抽出
  「このユーザーは省略を嫌う」
  ↓ 原則昇格
  「Reyn は省略せず完全な出力を提供する」

→ エピソード A と B は廃棄可能（原則に包含された）
```

---

## Reyn での実装可能性スケッチ

> **注**: これは実装提案ではなく思考実験です。

### `consolidate-memory` スキルへの拡張

現在の `consolidate-memory` は記憶の要約・統合を行う。
ここに value score を付与する仕組みを追加できる。

```yaml
# workspace/memory_score.yaml（仮）
entries:
  - id: "feedback_001"
    text: "ユーザーは箇条書きを好む"
    cache_value: 0.7        # 再導出コスト高い（ユーザー分析に時間がかかる）
    behavioral_sensitivity: 0.4  # 出力フォーマットに中程度の影響
    abstraction_depth: pattern   # エピソード → パターン 済み
    last_activated: 2026-05-12
    contradiction_count: 1
```

スコアが低いエントリを定期的に pruning するバッチを実行できる。

### P6 イベントログによる Sensitivity 計測（将来）

```python
# 概念コード（未実装）
def estimate_behavioral_sensitivity(memory_id: str, event_log: EventLog) -> float:
    """
    同一スキル・同類入力で memory が有効だったセッションと
    無効だったセッションの final_output の差異を計測する。
    """
    with_memory = event_log.query(memory_loaded=memory_id)
    without_memory = event_log.query(memory_loaded=None, similar_input=True)
    delta = compute_output_delta(with_memory, without_memory)
    return delta / storage_cost(memory_id)
```

`compute_output_delta` をどう定義するかが最大の設計課題。
LLM-as-judge、ルールベース比較、ユーザーフィードバック等が候補。

---

## 既存研究との比較

| システム | 記憶管理の軸 | 忘却の判断 | 直接計測 |
|---|---|---|---|
| MemGPT / Letta | 容量制限（context window） | 容量超過時に要約 | ✗ |
| ChatGPT Memory | ユーザー申告 + 推測 | ユーザーが手動削除 | ✗ |
| Hermes Agent | 時間 + 意味カテゴリ | タイムアウト | ✗ |
| **Reyn（目標）** | **Behavioral Sensitivity** | **3 条件（証拠/コンテキスト/包含）** | **✓（P6 で）** |

Behavioral Sensitivity の直接計測は、筆者が調査した範囲で **どの agent フレームワークも実装していない**。
これが genuine な差別化になりうる根拠。

---

## 未解決の問い（Open Questions）

このセクションは積極的に育てていく。

### Q1 — Behavioral Sensitivity の現実的な計測方法は？

A/B 比較が理想だが、同一ユーザーの同一タスクで「記憶あり/なし」を試すのはユーザー体験を損なう。

候補アプローチ:
- **事後的差分**: 記憶の内容が変わったタイミングの前後で出力を比較
- **LLM-as-judge**: 「この記憶が最終出力に影響したか」を判定させる
- **ユーザーフィードバック proxy**: NEVER/ALWAYS 判定を sensitivity として使う

### Q2 — 忘却タイミングの自動化 vs 人間の判断

Sensitivity が低くなったら自動廃棄するのか、人間に確認するのか。
Reyn の Permission model（ask_user）が使えるはず。

候補: `memory.on_prune: auto | ask_user | disabled`（FP-0006 の `on_propose` と対称）

### Q3 — Abstraction Depth の昇格タイミング

「3 件同じエピソードが来たらパターン化する」という閾値ベースが素直。
しかし質的な変化（「これは一般則だ」という判断）は LLM に委ねる方が自然。

Reyn の設計原則（決定論的に書けるものは deterministic に処理）を適用すると:
- 「N 件以上の共通エピソードが存在する」= 昇格候補の**検出**（deterministic）
- 「本当に昇格すべきか」= **判断**（LLM or ask_user）

### Q4 — 価値スコアの時間変化

Cache Value は時間とともに変わる（古い情報は再導出コストが下がることもある）。
スコアを静的に計算するのか、decay 係数を持つのか。

### Q5 — `feedback/` エントリの Abstraction Depth 分類

現在の memory ファイルは Abstraction Depth が混在している:
- `feedback_pre_conclusion_observation_checklist.md` → **原則**（最も抽象度高い）
- `session_resume_2026_05_10.md` → **エピソード**（具体的な作業状況）
- `project_residuals.md` → **パターン**（複数セッションにまたがる積み残し）

これを分類 → 廃棄候補（エピソード系、原則に包含済み）を明示できるか？

---

## 参照

- **Reyn P6**: `docs/concepts/events.md` — Behavioral Sensitivity 計測の基盤
- **FP-0006**: `docs/deep-dives/proposals/0006-skill-self-improvement.md` — `skill_version_hash` でバージョン間挙動比較（同じ思想）
- **FP-0009**: `docs/deep-dives/proposals/0009-operational-intelligence.md` — event log の RAG インデックス化（計測インフラの一部）
- **`consolidate-memory` スキル**: 実際の記憶統合の実装例
- **Hermes Agent 競合分析**: `docs/deep-dives/research/competitive/hermes-agent.md` — 永続メモリの先行実装例
