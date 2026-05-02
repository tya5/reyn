---
type: concept
topic: architecture
audience: [human, agent]
---

# Reliability Engineering

agent を障害から回復させること: スキーマ検証、拒否時の再プロンプト、ループ上限、ステップごとのタイムアウト、そして（より長期的には）リトライポリシーとチェックポイント/再開。目標は「LLM が間違えても、システムが定義された状態にとどまること」です。

## Reyn の実装方法

### 検証 + 再プロンプト

すべての LLM 出力は、副作用が実行される前に固定の検証パイプラインを通過します:

1. **正規化。** レスポンスをコントラクト JSON としてパースしようとします。失敗 → `normalization_error` イベント、再プロンプト。
2. **制御エンベロープの検証。** `type`/`decision`/`next_phase` の整合性チェック。失敗 → `validation_error`、再プロンプト。
3. **artifact の検証。** 選択したターゲットの入力スキーマに対して artifact をチェック。失敗 → `validation_error`、再プロンプト。
4. **Control IR の検証。** op の形式と Permission のチェック。失敗 → `control_ir_validation_error`、再プロンプト。

OS は不正な出力を黙って修正することはありません。設定可能な失敗再プロンプト回数を超えるとランが中止されます。失敗はイベントログで確認できます。

各リトライは `phase_retry` イベントを発行します。リトライカウンターは Phase 訪問ごとであり、3 回試行が必要な Phase は通常の出来事です。信頼性の問題は無制限のリトライであり、OS はそれを防ぎます。

### ループ上限と Phase バジェット

`reyn.yaml` の `limits.phase` 配下で設定される 2 つの補完的な上限が各 Phase に適用されます:

- **`max_visits`**（デフォルト `25`、`0` = 無制限）は、1 回のランで任意の単一 Phase が再訪問できる回数の上限です。超過すると OS は `loop_limit_exceeded` を発行し、ステータス `loop_limit_exceeded` でランを終了します。
- **`max_wall_seconds`**（デフォルト `0` = 無制限）は Phase 訪問ごとのウォールクロックバジェットを設定します。チェックは*ソフト*です: OS はリトライ/ターンの境界で経過時間を評価し、呼び出しの途中でキャンセルしません。超過すると OS は `phase_budget_exceeded` を発行し、ステータス `phase_budget_exceeded` でランを終了します。進行中の作業がキャンセルされないため、Workspace の状態は一貫して保たれます。

両方とも `--max-phase-visits` と `--phase-budget` でランごとにオーバーライドできます。

これにより以下から保護されます:

- LLM が満たせない基準による修正ループ（基準が到達不可能）。
- LLM が同じブランチを選び続けられるグラフ。
- 2 つの Phase が無限にピンポンする微妙なバグ。
- 実際に終了するが長すぎて有用でない Phase（遅い LLM、暴走する preprocessor チェーン）。

### LLM 呼び出しタイムアウトと一時的エラーのリトライ

各 LLM HTTP 呼び出しは LiteLLM を通じて渡される呼び出しごとのタイムアウト（`limits.llm.timeout`、デフォルト `60` 秒）と、一時的な障害（`429`、`5xx`、ネットワークリセット）に対する LiteLLM の組み込み指数バックオフリトライ（`limits.llm.max_retries`、デフォルト `3`）を持ちます。アプリケーションレベルの拒否（検証、正規化）は上記の再プロンプトループで別途処理されます。これらは異なる障害モードであり、バジェットを共有しません。

### Python preprocessor タイムアウト

`python` preprocessor ステップごとに、サブプロセス経由でウォールクロック `timeout`（デフォルト `30` 秒）が強制されます。タイムアウト時、親プロセスは子プロセスを SIGKILL し、ステップが失敗します。失敗は LLM に対してステップ結果として表面化し、LLM が反応できます。タイムアウトは偶発的に計算負荷の高い preprocessor 関数（正規表現の壊滅的なバックトラッキング、ユーザーコードの無限ループ）から保護します。

### 障害の可視性

すべての信頼性イベントが JSONL ログに記録されます:

| イベント | 内容 |
|-------|---------------|
| `validation_error` | OS が artifact/制御エンベロープを拒否 |
| `normalization_error` | OS が LLM レスポンスを全くパースできなかった |
| `control_ir_validation_error` | OS が op を拒否 |
| `phase_retry` | 拒否された出力のリトライ |
| `loop_limit_exceeded` | 訪問回数の上限に達した |
| `phase_budget_exceeded` | 現在の Phase のウォールクロックバジェットに達した |
| `phase_failed` | Phase が回復不能なエラーを発生させた |

`reyn events <log> --filter validation_error --filter normalization_error` で問題箇所に直接ジャンプできます。

## まだ薄い部分

いくつかの信頼性プリミティブは今日意図的にシンプルで、深化はロードマップにあります:

**リトライポリシーは分かれているが深くない。** アプリケーションレベルの拒否は検証エラーをフィードバックとして注入しながら `max_phase_retries`（デフォルト `2`）回まで再プロンプトします。ジッターなし、障害種別ごとの戦略なし。一時的な HTTP エラーには `limits.llm.max_retries` を通じた LiteLLM の組み込み指数バックオフが適用されます。2 つのパスはステートを共有しません。これは正しい形ですが、どちらもまだバックオフやエラーごとのポリシーをカスタマイズできません。

**Phase バジェットはソフトであり、ハードなキャンセルではない。** `limits.phase.max_wall_seconds` はリトライ/ターンの境界でチェックします。非常に長い LLM 呼び出しや preprocessor ステップは 1 回の操作分だけバジェットを超過することがあります。これは「ハードな保証」より「一貫した Workspace の状態」を優先するトレードオフであり、ほとんどのワークフローにとって正しいデフォルトです。呼び出し途中のキャンセルはオプトインモードとしてロードマップに存在します。

**チェックポイント/再開なし。** すべての状態変化がイベントであるため（P3）、再開に必要な*情報*はすでにログに存在します。しかし、イベント N で状態を再ロードして続行する*機構*はまだ構築されていません。追加するには新しいイベント型は必要ありません。ただイベントを単にレンダリングではなく状態復元として再生するランタイムモードが必要です。

**冪等性は Skill 作者の責任。** Phase が Control IR 経由でファイルを書き込む場合、リトライで Phase に再入すると再度書き込まれます。preprocessor と Control IR の区別が役立ちます（preprocessor は決定論的）が、外部から見える副作用を持つ Skill は冪等性について自ら考える必要があります。

## 関連情報

- [リファレンス: events](../../reference/runtime/events.md) — 完全なイベント分類
- [リファレンス: llm-output-contract](../../reference/runtime/llm-output-contract.md)
- [リファレンス: common-flags](../../reference/cli/common-flags.md) — `--max-phase-visits`、`--phase-budget`、`--llm-timeout`、`--llm-max-retries`
- [リファレンス: reyn.yaml](../../reference/config/reyn-yaml.md) — `limits` ブロック
- [ハウツー: events によるデバッグ](../../how-to/debug-with-events.md)
- [evaluation-and-observability.md](evaluation-and-observability.md) — 障害率の測定
- [tool-contract-design.md](tool-contract-design.md) — 検証される内容
