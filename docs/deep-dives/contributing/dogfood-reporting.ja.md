---
type: contributing
topic: dogfood-reporting
audience: [human, agent]
---

# Dogfood Batch レポーティング

[`dogfood-discipline.md`](dogfood-discipline.md) の姉妹ドキュメントです。同ドキュメントは 9 原則フレームワークと A1–A5 iterative loop を扱います。本ドキュメントは**結果をどこに記録するか**と**チームへどう共有するか**を扱います。

---

## 1. 3 つのレポーティング層

各 batch は 3 つの異なる層で出力を生成します。各層は異なる目的と対象読者を持ちます。

| 層 | 場所 | 目的 | 対象 |
|----|------|------|------|
| **詳細データ** | `docs/deep-dives/journal/dogfood/<batch-dir>/` | 完全な記録: メトリクス / per-scenario 判定 / 学習 | メンテナー、後続 batch、エージェント |
| **GitHub ヘッドライン** | GitHub Discussions → `Dogfood batches` | チームへ結果を共有; 議論の起点 | チーム、ステークホルダー |
| **Actionable findings** | GitHub Issues (`dogfood-finding` ラベル) | バグを severity 付きで追跡; fix PR にリンク | メンテナー、fix-wave エージェント |

3 層は相互補完します。Discussion ヘッドライン → journal commit をリンク、各 Issue → Discussion スレッドをリンク、journal retrospective → 両方を参照。

---

## 2. Per-batch journal エントリ (詳細データ)

### 場所

```
docs/deep-dives/journal/dogfood/<YYYY-MM-DD-batch-N-<topic>>/
```

例:

```
docs/deep-dives/journal/dogfood/2026-05-16-batch-26-fp-0034-n5-stability/
docs/deep-dives/journal/dogfood/2026-05-17-batch-27-chat-router-smoke/
```

ディレクトリ名は `日付 + batch 番号 + topic-slug` でエンコードします。topic slug は何をテストしたかの簡潔な記述子です。日付の範囲内でグローバルにユニークである必要はありません。

### ファイル

#### `summary.md` — ヘッドラインメトリクス (30–50 行)

summary は読者が最初に開くファイルです。batch の識別情報、ヘッドラインメトリクス、フレームワークバージョン、ベースライン比較を含みます。2 分以内で読めることを目標とします。

```markdown
# Batch 27 — Summary

**Date**: 2026-05-17
**Batch ID**: B27
**Topic**: chat router smoke + stdlib core
**Framework version**: FP-0036 `<commit_hash>`
**Scenario sets**: chat_router_smoke (7 scenarios) + stdlib_skills_core (9 scenarios)

---

## ヘッドラインメトリクス

| Metric | Value |
|--------|-------|
| 総実行数 | 16 (= 16 scenarios × N=1) |
| Verified | 12 / 16 = 75% |
| Inconclusive | 3 |
| Refuted | 1 |
| Blocked | 0 |
| Brier score | 0.21 |
| Wall-clock | ~12 min |
| LLM cost (est) | ~$0.04 |

## ベースライン比較

| Metric | Baseline (B26) | 本 batch (B27) | Delta |
|--------|---------------|----------------|-------|
| Verified % | 91.4% | 75% | -16.4pp |
| Brier | 0.177 | 0.21 | +0.033 |
| Regressed scenarios | — | 1 (`simple_capability_question`) | — |

> **注意**: B27 は B26 と異なる scenario set をカバーしています (chat_router_smoke vs FP-0034 wrapper-only)。
> verified rate の低下は B26 scenarios の regression ではなく、新しい scenario coverage を反映します。

## B26 からの carry-over

- B26-S3-NOOP-1 (invoke_action visibility gap) — LOW priority、 延期

## 次のステップ

- `simple_capability_question` refuted outcome の `[dogfood B27]` issue を起票
- fix wave 候補: [あれば記載]
```

#### `findings.md` — per-scenario 判定テーブルとバグエントリ

findings ファイルは完全な verdict matrix と CRITICAL / HIGH / MED / LOW に分類された finding のエントリを含みます。verdict matrix フォーマット:

```markdown
# Batch N — Findings

> batch の1行サマリー。

---

## 0. Run summary

| Item | Value |
|------|-------|
| Branch HEAD | `<commit_hash> <message>` |
| Tests | N passed / N skipped |
| Total runs | N |
| Wall-clock | ~N min |
| LLM cost (est) | ~$N |
| Driver | `<script or reyn dogfood run command>` |

---

## 1. Verdict matrix

| Scenario | V/I/R/B | Verified % | Status |
|----------|---------|-----------|--------|
| **S1** (説明) | N/N/N/N | N% | ✅ / ⚠️ / ❌ |

**Gate**: N=5 で全 scenario ≥80% verified — 達成 / 未達成。

---

## 2. Findings

### B<N>-<S-ID>-<seq> (<severity>) — <短い説明>

**Severity**: CRITICAL / HIGH / MED / LOW

**Observation**: [観測内容]

**Impact**: [ユーザー可視の影響]

**Carry-over**: [fix 計画または延期の理由]
```

Severity 定義 (dogfood-discipline.md §2 A4 と同一):

| Severity | 意味 |
|----------|------|
| CRITICAL | システム機能不全 |
| HIGH | コアユーザーパスが blocked |
| MED | 機能低下、回避策あり |
| LOW | 外観上の問題またはエッジケース |

#### `retrospective.md` — レッスンと原則のまとめ

retrospective は batch の恒久的な出力物です。固定の構造を持ちます:

```markdown
# Batch N — Retrospective

> 1行のマイルストーンサマリー。

---

## 1. Expected vs actual

| Scenario | Baseline | Prediction | Actual | Hit/Miss |
|----------|----------|-----------|--------|----------|
| S1 | V=N/N | V≥N/N | V=N/N | ✅ hit / ❌ miss |

**Batch Brier**: N.NNN

---

## 2. Turning points

### TP1: <名称>

[何が起きたか、なぜ重要だったか]

**Lesson**: [原則のまとめ]

---

## 3. 強化 / 新確立された原則

[本 batch で確認または新規確立された原則のリスト]

---

## 4. 次の batch への申し送り

### fix wave 候補

[fix wave に入る HIGH / CRITICAL findings のリスト]

### Optional carry-over (LOW / MED、 延期)

[優先度の理由とともにリスト]

---

## 5. Cost summary

| Item | Wall-clock | LLM cost (est) |
|------|-----------|---------------|
| ... | ... | ... |
| **Total** | | |
```

#### `report.json` — 機械可読の実行記録

`reyn dogfood run` が batch journal ディレクトリに人間可読ファイルと並べて書き出します。構造化された形式で同じ実行データを記録し、自動的な比較と履歴追跡に使用します。

スキーマ: 下記 [セクション 5](#5-reportjson-の読み方) を参照。

---

## 3. GitHub Discussions ヘッドライン

### カテゴリ設定 (オペレーター、一度だけ)

最初の batch Discussion を投稿する前に、チームオペレーターが GitHub UI でカテゴリを作成する必要があります:

1. **Discussions → New category** へ移動
2. Name: `Dogfood batches`
3. Description: `Batch-by-batch dogfood result threads`
4. Format: **Open-ended discussion**

カテゴリが作成されるまでは **General** をフォールバックとして使用します。カテゴリ作成後はリンクを更新してください。

### タイトルフォーマット

```
Batch N (YYYY-MM-DD): <topic> — <verified_rate>% verified, <regressed_count> regressed
```

例:

```
Batch 27 (2026-05-17): chat router smoke + stdlib core — 75% verified, 1 regressed
Batch 26 (2026-05-16): FP-0034 wrapper-only N=5 stability — 91% verified, 0 regressed
```

### 本文テンプレート

```markdown
**Batch N — YYYY-MM-DD — <topic>**

- Framework: FP-XXXX framework `<commit_hash>`
- Scenario sets: <set_name> (N) + <set_name> (N)
- Verified: N/N = N%
- Inconclusive: N
- Regressed (vs baseline `b<N>`): N (= `<scenario_id>` があれば)
- Brier vs prediction: N.NN
- Journal: <summary.md の commit へのリンク>
- Fix-wave PRs: <リンク、またはまだ未対応なら "none yet">

[discussion follows in comments]
```

#### 例 (プレースホルダー埋め済み)

```markdown
**Batch 27 — 2026-05-17 — chat router smoke + stdlib core**

- Framework: FP-0036 framework `a1b2c3d`
- Scenario sets: chat_router_smoke (7) + stdlib_skills_core (9)
- Verified: 12/16 = 75%
- Inconclusive: 3
- Regressed (vs baseline `b26`): 1 (= `simple_capability_question`)
- Brier vs prediction: 0.21
- Journal: https://github.com/tya5/reyn/commit/<sha>
- Fix-wave PRs: none yet

[discussion follows in comments]
```

### 末尾コメント: issue インデックス

本 batch の全 `dogfood-finding` Issue を起票したあと、Discussion スレッドに末尾コメントとして Issue のリストを追加します:

```markdown
**このバッチから派生した Issues:**

- #42 [dogfood B27] simple_capability_question: refuted — reply がケイパビリティリストを省略 [HIGH]
- #43 [dogfood B27] stdlib_core_S3: inconclusive — noop 下でスキーママ不整合 [MED]
```

これにより Discussion スレッドが batch のシングルナビゲーションハブとなります。

---

## 4. Actionable findings の GitHub Issues

### ラベル

dogfood 起因のバグは全て `dogfood-finding` ラベルを使用します。最初の issue を起票する前に GitHub UI (Labels ページ) でラベルを作成してください。

### タイトルの severity 表記

issue リストで開かずに severity が見えるようタイトルにブラケット表記を含めます:

| Severity | タイトル表記 |
|----------|------------|
| CRITICAL | `[CRITICAL]` |
| HIGH | `[HIGH]` |
| MED | `[MED]` |
| LOW | `[LOW]` (INFO エントリは Issue 起票不要) |

### タイトルフォーマット

```
[dogfood B<N>] <scenario_id>: <symptom> [<SEVERITY>]
```

例:

```
[dogfood B27] simple_capability_question: reply がケイパビリティリストを省略 [HIGH]
[dogfood B27] stdlib_core_S3: noop backend 下でスキーマ不整合 [MED]
[dogfood B26] S3-noop: invoke_action が D14 visibility gate をバイパス [LOW]
```

### 本文テンプレート

```markdown
## Source

- Batch: B<N> — <YYYY-MM-DD>
- Scenario: `<scenario_id>`
- Discussion: <Discussion スレッドへのリンク>
- Run: `<run_id または commit>`

## Observed vs expected

**Observed**: [何が起きたかの具体的な説明]

**Expected**: [何が起きるべきだったか、該当する spec/docs への参照]

## Event log excerpt

```jsonl
{"type": "<event_type>", "data": {...}, "ts": "..."}
```

## Severity

**<SEVERITY>**: [1文の根拠]

## Fix hypothesis

[根本原因の初期仮説 — 確定ではなく仮説として記載]

## Acceptance criteria

- [ ] Scenario `<scenario_id>` が N=3 で `verified` を返す
- [ ] 関連 scenario に regression なし
```

### クロスリンク規律

- 全 `dogfood-finding` Issue は本文の `## Source` で Discussion スレッドにリンクします。
- Discussion スレッドは末尾コメントで派生 Issue を集約します (セクション 3 参照)。

---

## 5. `report.json` の読み方

`report.json` は `reyn dogfood run` が batch journal ディレクトリに書き出します。人間可読ファイルと並置されます。

### スキーマ

```json
{
  "run_id": "<uuid>",
  "scenario_set_name": "<name>",
  "started_at": "<ISO 8601>",
  "completed_at": "<ISO 8601>",
  "framework_version": "<commit_hash or semver>",
  "n": "<repetitions per scenario>",
  "scenarios": [
    {
      "id": "<scenario_id>",
      "outcome": "verified | inconclusive | refuted | blocked",
      "repetitions": [
        {
          "run": "<repetition index>",
          "outcome": "verified | inconclusive | refuted | blocked",
          "reply_verdict": "pass | fail | inconclusive",
          "events_verdict": "pass | fail | inconclusive",
          "artifacts_verdict": "pass | fail | inconclusive"
        }
      ],
      "outcome_prediction": {
        "verified": 0.0,
        "inconclusive": 0.0,
        "refuted": 0.0,
        "blocked": 0.0
      },
      "brier": "<float>"
    }
  ],
  "aggregate": {
    "verified": "<count>",
    "inconclusive": "<count>",
    "refuted": "<count>",
    "blocked": "<count>",
    "total": "<count>",
    "verified_pct": "<float 0–100>"
  },
  "brier": "<float — batch 平均 Brier score>"
}
```

### フィールドリファレンス

| フィールド | 型 | 意味 |
|-----------|----|----|
| `run_id` | UUID 文字列 | グローバルユニークな実行識別子。`.reyn/dogfood/runs/<run_id>/` に対応 |
| `scenario_set_name` | 文字列 | scenario set YAML の名前 (例: `chat_router_smoke`) |
| `started_at` / `completed_at` | ISO 8601 | 実行の wall-clock 境界 |
| `framework_version` | 文字列 | 実行時の commit hash または semver タグ |
| `n` | int | scenario あたりの繰り返し数 (`--n` フラグと同じ) |
| `scenarios[].outcome` | enum | 当該 scenario の全繰り返しでの worst-case outcome |
| `scenarios[].brier` | float | Per-scenario Brier score (`outcome_prediction` 宣言が必要) |
| `aggregate.verified_pct` | float | `verified / total * 100` |
| `brier` | float | prediction を持つ全 scenario の平均 Brier |

`brier` フィールドは、`outcome_prediction` を宣言した scenario がない場合は `null` です。

### report.json をツールで扱う

```bash
# batch Brier score を取得
jq '.brier' report.json

# refuted scenarios をリスト
jq '[.scenarios[] | select(.outcome == "refuted") | .id]' report.json

# verified percentage
jq '.aggregate.verified_pct' report.json
```

---

## 6. 関連ドキュメント

- [`dogfood-discipline.md`](dogfood-discipline.md) — 方法論層: 9 原則フレームワーク、A1–A5 iterative loop、scenario 設計
- [`dogfood-regression-playbook.md`](dogfood-regression-playbook.md) — ステップバイステップの実行手順、regression トリアージ、fix-wave dispatch (R2 担当)
- [`reference/cli/dogfood.md`](../../reference/cli/dogfood.md) — CLI リファレンス: `reyn dogfood run`、`compare`、`baseline`
- [`concepts/observability/dogfood-scenarios.md`](../../concepts/observability/dogfood-scenarios.md) — scenario set YAML スキーマの authority
