---
type: contributing
topic: dogfood-regression-playbook
audience: [human, agent]
---

# Dogfood リグレッション・プレイブック

FP-0036 dogfood シナリオスイートを Reyn リリースをまたいでリグレッション確認として実行するための手順書です。姉妹ドキュメント:

- `dogfood-discipline.md` — 方法論レイヤー (= 9 原則, A1-A5 ループ)
- `dogfood-reporting.md` — レポーティングレイヤー (= journal + Discussions + Issues)

本プレイブックは**オペレーショナルレイヤー**です: どのコマンドを、どの順序で実行し、各ゲートで何を判断するか、を記述します。

---

## リグレッションスイートを実行するタイミング

以下のいずれかに該当するときは、フルシナリオスイートを実行します。

- **リリースタグを打つ前** — ユーザーに届くバージョンタグすべて。
- **OS レイヤーのコードに触る PR をマージした後** — `src/reyn/op_runtime/`、`src/reyn/kernel/`、`src/reyn/chat/`。OS の構造的変更は、ユニットテストが捉えられないルーティング挙動を静かに壊すことがあります。
- **stdlib スキルのプロンプト内容に触る PR をマージした後** — `skill.md` や phase の `instructions` フィールドの変更は、LLM のルーティングと出力に影響します。
- **四半期ごと** — フルカバレッジ実行 + シナリオ YAML 内の全 `outcome_prediction` 分布の再キャリブレーション。

リスクの低い PR (ドキュメント、config スキーマ、ツーリングのみ) の後は、`--n 1` で `chat_router_smoke.yaml` のみのスモーク実行で可です。OS またはプロンプトの変更後は `--n 5` の安定性実行が必須です。

---

## Step 0 — プリフライトチェックリスト

**目的**: LLM コストを消費する前に、テスト環境が隔離されて完全に動作していることを確認する。

```bash
# 1. SUT のコミットハッシュを記録する — 全レポートと Issue タイトルに含める。
git rev-parse HEAD

# 2. Python 依存関係がインポートできることを確認する。
pip list | grep -E "croniter|httpx|litellm"

# 3. LiteLLM proxy が localhost:4000 で動作していることを確認する。
curl -s localhost:4000/v1/models | python3 -m json.tool | head -20

# 4. このバッチ用の隔離済み作業ディレクトリを作成する。
mkdir -p /tmp/reyn-b<N>
cd /tmp/reyn-b<N>
reyn init          # 新しい .reyn/ 状態ディレクトリを作成する

# 5. 隔離が clean であることを確認する (前回のセッションが残っていないか)。
ls .reyn/           # init アーティファクトのみが存在するはず。過去の run は含まれない
```

**隔離ルール** (memory `feedback_dogfood_parallel_reyn_agent_isolation.md` 参照):

- 各リグレッションバッチは専用の `/tmp/reyn-b<N>/` cwd と新しい `.reyn/` 状態を持ちます。開発 cwd を再利用しないこと — セッション状態が run 間でリークします。
- 並列シナリオを sub-agent で実行するとき: 各 sub-agent は異なる `--agent-name` を使い、開発エージェントや他の並列 sub-agent とセッションが衝突しないようにします。
- `--storage /tmp/reyn-b<N>/.reyn/dogfood/runs/` を明示的に指定し、すべての run アーティファクトが隔離ディレクトリに格納されるようにします。開発ワークスペースには入れません。

Step 0 のいずれかのチェックが失敗した場合は、先に進まないこと。まず環境を修正します。

---

## Step 1 — ベースラインを記録する (初回セットアップまたは新しいリリースタグ)

ベースラインは、将来の candidate run と比較するための名前付き run スナップショットです。リリースサイクルごとに 1 回、または known-good 状態を保存する必要があるときに記録します。

```bash
# スモークスイートを安定性ショット数で実行する。
reyn dogfood run dogfood/scenarios/chat_router_smoke.yaml \
    --n 5 \
    --agent default \
    --storage /tmp/reyn-b<N>/.reyn/dogfood/runs/

# run が run_id (UUID) を出力する。それをベースラインとしてタグ付けする。
reyn dogfood baseline <run_id> --label v0.X-baseline
```

追跡したいシナリオセットごとに繰り返します:

```bash
reyn dogfood run dogfood/scenarios/stdlib_skills_core.yaml --n 5
reyn dogfood baseline <run_id> --label v0.X-stdlib-baseline

reyn dogfood run dogfood/scenarios/permissions_and_safety.yaml --n 5
reyn dogfood baseline <run_id> --label v0.X-permissions-baseline
```

**ベースラインラベルの命名規則**: SUT バージョンとセット名を含めることで、数ヶ月後に比較するときも曖昧さがなくなります。

| パターン | 例 |
|---|---|
| リリースタグ | `v0.3-chat-router` |
| 四半期 | `2026-Q2-stdlib` |
| マージ前ゲート | `pre-pr-42-permissions` |

ベースラインは `.reyn/dogfood/baselines/<label>/` 以下に run ディレクトリへのシンボリックリンクとして保存されます。MVP では per-developer (git で共有しない) です。CI で共有ベースラインが必要になった場合は再検討します。

---

## Step 2 — Candidate run (リグレッション計測)

Candidate run は、コード変更後に同じシナリオセットを新しく実行し、ベースラインとの比較で挙動が変わっていないかを計測するものです。

```bash
reyn dogfood run dogfood/scenarios/chat_router_smoke.yaml \
    --n 5 \
    --agent default \
    --storage /tmp/reyn-b<N>/.reyn/dogfood/runs/
```

`--n 5` は推奨の安定性ショット数です。batch 14 で確立した production-grade マイルストーン閾値 (N=5, ≥80% verified) に合わせています。高速スモークチェックには低い N でも可。高リスクのリリースゲートには `--n 10` まで引き上げます。

run 完了後、出力の candidate `run_id` を記録します — Step 4 で使います。

**複数のシナリオセットを実行する場合**: 順次実行するか (または別の `--agent-name` を使う並列 sub-agent で)、全 candidate `run_id` を記録します。

---

## Step 3 — リプレイモード (オプション、LLM コスト ゼロ)

`dogfood/fixtures/<set>/` 以下にフィクスチャが存在する場合 (前回の run で記録済み)、リプレイモードは LLM を一切呼ばずに検証を再実行します。決定論性が重要で LLM コストが許容できない CI ゲーティングに使います。

```bash
reyn dogfood run dogfood/scenarios/chat_router_smoke.yaml \
    --replay dogfood/fixtures/chat_router_smoke/
```

リプレイ run は `run_id` にタグが付くため、レポートでライブ run と区別できます。リプレイ run とライブベースラインを比較しないこと — 出力分布が直接比較できません。

**フィクスチャの新鮮さ**: マイナーリリースごとに再記録します。フィクスチャと現在のランタイムのスキーマ不一致は、自動的に再記録を強制します。手動で再記録するには:

```bash
# リリース後の最初のライブ run でフィクスチャが記録される:
reyn dogfood run dogfood/scenarios/chat_router_smoke.yaml --n 5
# フィクスチャは dogfood/fixtures/chat_router_smoke/ に自動的に格納される。
```

---

## Step 4 — ベースラインと比較する

```bash
reyn dogfood compare <baseline_label_or_run_id> <candidate_run_id> \
    --threshold 0.05
```

**終了コード**:

| コード | 意味 |
|---|---|
| `0` | リグレッションなし — verified-rate の低下が 5 pp 閾値以内。 |
| `1` | リグレッション警告 — verified-rate が `--threshold` を超えて低下した。 |
| `2` | エラー — run ディレクトリの一方または両方が見つからない。 |

**比較出力の読み方**:

```
  Baseline:  a1b2c3d4-...  (80.0% verified)
  Candidate: b2c3d4e5-...  (46.7% verified)
  Delta:     -33.3pp  /  threshold=-5.0pp
  Result:    REGRESSION ALERT

Regressed scenarios (2):
  - explicit_skill_invocation_word_stats: verified → refuted
  - catalog_routing_decided_emitted: inconclusive → refuted
```

トリアージすべきフィールド:

- `regressed_scenarios` — 最悪ケースの結果が悪化した各シナリオ。Step 5 のトリアージに進みます。
- `improved_scenarios` — 結果が改善したシナリオ。意図しない副作用がないか確認し、意図的かを確認します。
- `verified_rate_delta` — 集計変化量。±5 pp 以内はノイズ。−5 pp 超はリグレッションゲート失敗。

CI 連携用の JSON 出力:

```bash
reyn dogfood compare <baseline> <candidate> --json; echo "exit: $?"
```

---

## Step 5 — リグレッション・シナリオのトリアージ

`regressed_scenarios` に列挙されたシナリオごとに、対応を取る前に以下の 4 つのトリアージカテゴリに分類します。

### カテゴリ 1: 真のリグレッション

SUT が、観測可能な挙動を壊す方向に変化した。シナリオは設計通りに計測できており、その計測が失敗している。

**シグナル**: シナリオの `expected.events` または `expected.artifacts` ブロックが refuted — 構造的なベリファイアが失敗しており、LLM judge だけではない。

**対応**:
1. `dogfood-finding` Issue を起票する (`dogfood-reporting.md` のテンプレート参照)。
2. 重大度を `dogfood-discipline.md` Section A4 のタクソノミーで CRITICAL / HIGH / MED / LOW に割り当てる。
3. 重大度が HIGH 以上の場合はリリースをブロックする。ただし、Issue にブロック理由の根拠コメントが明示されて受け入れが確認された場合は除く。
4. 修正がマージされた後、修正後の candidate を実行し、リグレッションが解消されていることを確認する (Step 8 参照)。

### カテゴリ 2: シナリオのフレーク

シナリオの `expected.*` アサーションが LLM の確率的ばらつきに対して厳しすぎる。根本的な挙動は変わっておらず、ルーブリックが過剰に具体的。

**シグナル**: コード変更なしに N=5 run を繰り返すと、結果が `verified` と `refuted` の間でフリップする。失敗しているのは `judge` ベリファイアのみ (events と artifacts はパス)。

**対応**:
1. シナリオ YAML のルーブリックを緩める — 表現を広げる、基準数を減らす、substring/regex マッチを緩和する。
2. コミットメッセージに根拠を記述してコミットする:
   `fix(dogfood): loosen rubric for <scenario_id> — original too tight for model variance`
3. 更新した YAML で candidate を再実行し、フレークが解消されることを確認する。

### カテゴリ 3: キャリブレーションドリフト

シナリオの結果は `verified` (挙動は正しい) だが、`outcome_prediction` 分布が観測分布と合わなくなっている。N≥5 run を通じて Brier スコアが 0.5 を超えた状態が続く。

**シグナル**: `summary.json` でシナリオの Brier スコアが高いが、結果は `refuted` ではなく `verified` または `inconclusive`。

**対応**:
1. シナリオ YAML の `outcome_prediction` を観測分布に合わせて更新する。
2. コミット: `chore(dogfood): recalibrate outcome_prediction for <scenario_id>`
3. これは通常の四半期メンテナンス作業。バグ Issue は起票しない。

### カテゴリ 4: 環境依存

オペレーターの環境に前提条件が欠けているため、結果が `blocked`。MCP サーバーが未設定、Web サーバーが起動していない、または必要なパーミッションが付与されていない。

**シグナル**: `events.jsonl` に `blocked` 結果がタイムアウトまたは `permission_denied` イベントとして現れる。シナリオ YAML の `covers:` タグが環境依存機能を参照している。

**対応**:
1. バッチジャーナルの `findings.md` に不足している前提条件を記載する。
2. Issue は起票しない — これは環境のギャップであり、プロダクトのバグではない。
3. シナリオ YAML の `description` フィールドに前提条件の要件をドキュメント化するメモを追加する。

---

## Step 6 — カバレッジチェック

フルリグレッションパス後に実行し、前回のバッチ以降に追加されたカバーされていない機能を洗い出します。

```bash
reyn dogfood coverage dogfood/scenarios/*.yaml
```

出力例:

```
Total features:   187
Covered:           42  (22.5%)
Uncovered:        145

Uncovered (sample):
  os-core/llm-validation/artifact-schema-validation
  control-ir-ops/sandboxed-exec
  stdlib-skills/skill-builder
  ...
```

**判断ルール**:

- 現在のリリースサイクルで追加されたがカバーされていない機能: シナリオを作成するフォローアップタスクを起票する (バグ Issue ではない)。優先度: OS コアパスの機能は HIGH、それ以外は MED。
- FP-0036 マージ時に確立された 22.5% のベースラインは、シナリオが追加されるにつれて徐々に増加します。リリースをまたいで covered % のトレンドを追跡してください — 減少トレンドは、シナリオ作成が機能成長に追いついていないことを意味します。
- 未知の `covers:` タグ (= feature-map のパスに一致しないタグ) は警告として表示されます。`docs/feature-map.md` のパス体系 (小文字ケバブケース) に合わせてタグを修正します。

JSON 出力:

```bash
reyn dogfood coverage dogfood/scenarios/*.yaml --json
```

---

## Step 7 — レポート

レポート作成は `dogfood-reporting.md` に完全に従います。このステップはそちらに委譲します。ここでは順序付けのための要約のみを記載します。

```bash
# ジャーナルを書く前に run の 4 バンド内訳を出力する。
reyn dogfood report <candidate_run_id>
```

ジャーナルのステップ:

1. `docs/deep-dives/journal/dogfood/<YYYY-MM-DD-batch-N-<tag>>/` を作成する。
2. `docs/deep-dives/contributing/templates/` からテンプレートをコピーする。
3. `summary.md`、`findings.md`、`retrospective.md` を記入する。
4. run ディレクトリから `report.json` をジャーナルディレクトリに移動する。
5. `Dogfood batches` カテゴリで GitHub Discussion スレッドを開く。
6. HIGH 以上の重大度のファインディングごとに `dogfood-finding` Issue を起票する。

自律ドライバーの規律が適用されます (memory `feedback_dogfood_driver_role.md`): GitHub Discussion への投稿まで指示を待たずに自律的に完了させます。リリースをブロックする CRITICAL なファインディングが見つかった場合にのみ一時停止します。

---

## Step 8 — 修正ウェーブ (リグレッションが見つかった場合)

Step 5 カテゴリ 1 で真のリグレッションが特定された場合は、`dogfood-discipline.md` Section A5 に従って修正ウェーブをディスパッチします。

修正がマージされた後:

```bash
# 修正済み SUT に対して candidate を再実行する。
reyn dogfood run dogfood/scenarios/chat_router_smoke.yaml --n 5

# 修正前 candidate と修正後 candidate を比較する。
reyn dogfood compare <pre-fix-run-id> <post-fix-run-id>
```

確認: 新しい比較の `regressed_scenarios` が、修正対象のシナリオについて空になっていること。残っている場合は修正が完全に解消されていない — 反復します。

フィクスチャを再記録し、将来のリプレイ run が修正を反映するようにします:

```bash
# 修正後の run は既に新しいフィクスチャを記録している。
# フィクスチャディレクトリが更新されていることを確認する。
ls -lt dogfood/fixtures/chat_router_smoke/
```

修正によってイベントタイプ、スキーマフィールド、またはアーティファクトの形状が変わった場合、古いフィクスチャは次のリプレイ run で stale として検出されて自動無効化されます。次のライブ run より前にリプレイモードを使う必要がある場合は手動で再記録します。

---

## クイックリファレンス

| ステップ | コマンド |
|---|---|
| プリフライト | `git rev-parse HEAD` + `curl localhost:4000/v1/models` |
| ベースライン | `reyn dogfood run <set.yaml> --n 5 && reyn dogfood baseline <id> --label <name>` |
| Candidate | `reyn dogfood run <set.yaml> --n 5` |
| リプレイ | `reyn dogfood run <set.yaml> --replay <fixture_dir>` |
| 比較 | `reyn dogfood compare <baseline> <candidate> --threshold 0.05` |
| レポート | `reyn dogfood report <run_id>` |
| カバレッジ | `reyn dogfood coverage dogfood/scenarios/*.yaml` |
| 修正後確認 | `reyn dogfood compare <pre-fix-run> <post-fix-run>` |

**安定性ショット数のガイドライン**:

| コンテキスト | `--n` |
|---|---|
| 高速スモーク (低リスク PR) | `--n 1` |
| 標準リグレッション | `--n 5` |
| リリースゲート | `--n 5` (最低) |
| 高リスク / アトラクタ主張 | `--n 10` |

---

## クロスリファレンス

- `dogfood-discipline.md` — 方法論 (= 9 原則、A1-A5 ループ、Brier スコアリング)
- `dogfood-reporting.md` — レポーティングレイヤー (= journal + Discussions + Issues)
- `concepts/observability/dogfood-scenarios.md` — YAML スキーマ、4 バンド結果、カバレッジメカニズム
- `reference/cli/dogfood.md` — CLI サブコマンドリファレンス
- `proposals/0036-dogfood-scenario-framework.md` — 設計根拠とオープンポイント
- Memory `feedback_dogfood_driver_role` — 自律ドライバーの規律
- Memory `feedback_dogfood_parallel_reyn_agent_isolation` — per-cwd + per-agent 隔離
- Memory `feedback_pre_conclusion_observation_checklist` — ファインディング記述前のアクティブトリガー
