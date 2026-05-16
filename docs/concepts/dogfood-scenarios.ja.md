---
type: concept
topic: dogfood-scenarios
audience: [human, agent]
---

# Dogfood シナリオフレームワーク

Reyn のリリースをまたいで継続的に使用する回帰テスト用シナリオスイート。
reply テキスト・P6 イベント・ワークスペース成果物の 3 つの観測面に対してアサーションを宣言し、
`docs/feature-map.md` のカバレッジと照合する。
一回限りのバッチ事前準備ではなく、YAML で記述された再利用可能な回帰スイートとして設計されている。

## なぜ必要か

FP-0036 以前は 3 つの仕組みが統合されていなかった:

| | `reyn eval` | Dogfood シナリオフレームワーク |
|---|---|---|
| 起点 | `reyn run <skill>` | `reyn chat`（ルーターがスキルを選択） |
| 検証面 | フェーズ単位の rubric（LLM judge） | reply + events + artifacts |
| スコープ | 1 スキル単位 | feature-map カバレッジ |
| 結果スケール | 二値（pass/fail） | 4 バンド（verified / inconclusive / refuted / blocked） |
| 用途 | スキル単位の回帰 | システム全体の e2e 回帰 |

このフレームワークは `reyn eval` と **直交** している。`judge_output` op バックエンドと
ベースライン比較パターンは再利用するが、CLI とスキーマは独立している。
一回限りのバッチプレリュード（Markdown 散文）とも直交する。

4 つの設計制約:

- **確率論的挙動** — アサーションは安定性バンドを使用する（二値 pass/fail ではない）
- **コスト** — フルスイートの再実行は LLMReplay フィクスチャを使用（LLM コスト ゼロ）
- **挙動ドリフト** — `reyn dogfood compare <baseline> <candidate>` で回帰とノイズを区別
- **カバレッジ** — `reyn dogfood coverage` で feature-map の未カバー項目を一覧表示

## スキーマ

シナリオセットは `dogfood/scenarios/` 以下の YAML ファイル。
トップレベルの `covers:` はセット全体がカバーする機能を列挙する。
各シナリオの `covers:` はカバレッジマトリックスに反映される。

### シングルターンシナリオ

```yaml
type: dogfood_scenario_set
name: chat_router_smoke
description: チャットルーター意図振り分け + stdlib カタログディスパッチスモーク
covers:
  - chat-router/intent-routing
  - stdlib-skills/direct-llm

scenarios:
  - id: simple_greeting
    covers: [chat-router/intent-routing, stdlib-skills/direct-llm]
    input: "こんにちは、何ができますか?"
    expected:
      reply:
        kind: judge
        rubric:
          - 機能を高レベルで説明している
          - chat / スキル / エージェントに言及している
      events:
        must_emit:
          - { type: skill_run_spawned, count: ">=1" }
          - { type: skill_run_completed, status: success }
        must_not_emit:
          - { type: permission_denied }
      artifacts:
        - { skill: direct_llm, present: true }
    outcome_prediction:
      verified: 0.7
      inconclusive: 0.2
      refuted: 0.05
      blocked: 0.05
```

### マルチターンシナリオ

マルチターンは `input:` の代わりに `prompts: [...]` を使用する。
`expected` ブロックは `per_turn_expected` が指定されていない限り最終ターンに適用される。

```yaml
  - id: multi_turn_plan
    covers: [os-core/phase-engine/act-decide-loop]
    prompts:
      - "コードを改善してください"
      - "変更を適用してください"
    expected:
      events:
        sequence:
          - skill_run_spawned
          - skill_run_completed
```

`input` と `prompts` は相互排他。両方指定すると `ScenarioLoadError` が発生する。

## 検証面

3 つのベリファイアが独立して動作し、結果は最悪ケースで合成される:
1 つでも `refuted` になるとシナリオ全体が `refuted` となる。

### Reply

`kind` でマッチング方式を指定:

| kind | アサーション |
|---|---|
| `judge` | `rubric` の自然言語基準リストを `judge_output` op でスコアリング |
| `substring` | `value` 文字列が reply のどこかに含まれる |
| `exact` | `value` 文字列が reply に完全一致する（前後の空白は無視） |
| `regex` | `value` パターンが `re.search` でマッチする |

### Events

`must_emit` はイベントの存在をアサート（カウント比較子 `>=1`、`==2`、`<5` 等、ペイロードのサブセットマッチ対応）。
`must_not_emit` は不在をアサート。`sequence` はイベントタイプの順序付き部分列をアサート。

### Artifacts

各 `ArtifactAssertion` はワークスペース状態をテスト: `skill` / `type` による存在確認と、
オプションで `fingerprint`（正規化コンテンツの SHA256）によるピン留め回帰テスト。

## 4 バンド結果

各ベリファイアは 4 バンドのいずれかを返す:

- `verified` — アサーション明確に通過
- `inconclusive` — 判断に十分なシグナルなし
- `refuted` — アサーション明確に失敗
- `blocked` — インフラ障害（タイムアウト、フィクスチャ不在など）

Events / artifacts ベリファイアの結果が優先される。`judge` は `inconclusive` 時のタイブレーカー。
バンドのセマンティクスと Brier スコアリングの詳細は
[Dogfood discipline](../deep-dives/contributing/dogfood-discipline.ja.md) を参照。

`outcome_prediction` は期待する 4 バンド確率分布（合計 1.0 ± 0.001）を宣言する。
Brier スコアが複数回の実行にわたるキャリブレーション品質を測定する。

## カバレッジ

各シナリオの `covers:` タグは `docs/feature-map.md` のフィーチャーパスにマッピングされる。
パス体系は小文字ケバブケース:

```
### OS Core          -> os-core
#### Phase Engine    -> os-core/phase-engine
| Act/Decide loop |  -> os-core/phase-engine/act-decide-loop
```

`reyn dogfood coverage`（機械可読出力は `--json`）は全シナリオセットを読み込んで報告する:

```
Total features:   187
Covered:           42  (22%)
Uncovered:        145

Uncovered (sample):
  os-core/llm-validation/artifact-schema-validation
  control-ir-ops/sandboxed-exec
  stdlib-skills/skill-builder
  ...
```

フィーチャーパスに一致しない未知のタグは警告として表示されるが、実行は失敗しない。

## 回帰ワークフロー

```bash
# ベースラインを記録
reyn dogfood run dogfood/scenarios/chat_router_smoke.yaml --n 5 --baseline smoke-v1

# Reyn 変更後にキャンディデートを実行して比較
reyn dogfood run dogfood/scenarios/chat_router_smoke.yaml --n 5
reyn dogfood compare smoke-v1 <run_id>
```

`compare` は回帰したシナリオ、新たに通過したシナリオ、Brier ドリフトを報告する。
いずれかのシナリオが `--threshold`（デフォルト 0.1）を超えて回帰した場合、終了コード 1 を返す。

## リプレイモード

初回実行時のフィクスチャは LLMReplay 統合により `dogfood/fixtures/<scenario_id>/` に記録される。
`--replay <fixture_dir>` 付きの後続実行では記録済みの LLM レスポンスを使用する
（LLM コストゼロ、完全決定論的）:

```bash
reyn dogfood run dogfood/scenarios/chat_router_smoke.yaml --replay dogfood/fixtures/
```

リプレイモードの実行は `run_id` にタグが付くため、レポートでライブ実行と区別できる。
フィクスチャはリリースタグごとに再記録される。スキーマ不一致は自動的に再記録を強制する。

リプレイは `src/reyn/testing/replay.py`（`LLMReplay`）上に構築されている。
フィクスチャ記録の詳細は
[リプレイテストガイド](../guide/for-reyn-developers/write-replay-tests.md) を参照。

## 相互参照

- [Reference: `reyn dogfood` CLI](../reference/cli/dogfood.md) — サブコマンドリファレンス
  （run / coverage / report / compare / baseline）
- [Dogfood discipline](../deep-dives/contributing/dogfood-discipline.ja.md) —
  4 バンド結果スケール・Brier スコアリング・9 原則フレームワーク
- [Concepts: Evaluation](evaluation.ja.md) — `reyn eval`（スキル単位 rubric、直交する検証面）
- [Concepts: Events](events.ja.md) — `must_emit` アサーションで使用する P6 イベントタイプ
- [Concepts: Operational Intelligence](operational-intelligence.ja.md) — 同 P6 イベントログのインデックス・クエリ
- [Feature Map](../feature-map.md) — カバレッジタグの正典
