---
type: concept
topic: runtime
audience: [human, agent]
search_hints: [config hot-reload, hot-reload, IN-set, OUT-set, HotReloader, hooks_add, /reload, mcp.yaml, cron.yaml, hooks.yaml, reyn.yaml, turn boundary, config_reloaded, reapply seam, hooks layer, per-agent hooks, write-gate]
---

# Config ホットリロード

Reyn のコンフィグは、ミュータビリティのルールが異なる 2 つのセットに分かれています。ホットリロード機能は、プロセスを再起動することなく、ランタイムでミュータブルなセットをセーフポイントで再読み込みします。

## IN-set vs OUT-set（ライトゲート境界）

| セット | ファイル | ミュータブルなタイミング |
|--------|---------|----------------------|
| **IN-set**（ランタイムミュータブル） | `.reyn/config/mcp.yaml`、`.reyn/config/cron.yaml`、`.reyn/config/hooks.yaml` | ターン境界でのホットリロード |
| **OUT-set**（再起動のみ） | `reyn.yaml`（セキュリティ / パーミッション / サンドボックス / バジェット / ループバルブ） | プロセス再起動のみ |

境界は構造的なものです: `load_hot_reload_config` は `.reyn/config/*.yaml` の IN-set ファイルのみを開きます。ホットリロード — および それをトリガーする LLM-op — は、ローダーがそれらのファイルを開かないため、OUT-set に触れることは構造上不可能です。

## HotReloader の仕組み

### ターン境界セーフポイント（timing-B）

トリガーは `request_reload(source=…)` を呼び出してリロードを**スケジュール**しますが、即時には適用しません。リロードは `apply_pending()` で適用されます。これはターン境界（finish-reason=stop — `turn_end` セーフポイント）で呼び出されます。1 ターン内の複数のトリガーは 1 回の適用に集約されます: **1 ターン = 1 コンフィグスナップショット**。次のターンは新しいコンフィグで実行されます。

### 適用前バリデーション（validate-before-apply）

リアプライシームが実行される前に、IN-set の構造チェックが行われます。不正な IN-set（cron ジョブの形式が不正、hooks YAML が不正など）は**リロード全体を拒否**します — シームは実行されず、ライブコンフィグは変更されません。拒否時には `config_reloaded` P6 イベントは発行されません（状態変化が発生していないため）。

### P6 イベント

成功した適用後、`config_reloaded` が以下の情報と共に発行されます:

- `source` — `"operator"`（`/reload` 経由）または `"llm_op"`（`hooks_add` 経由）
- `components` — 変更されたシーム名のリスト
- `failed` — 例外が発生したシーム名のリスト

すべてのコンフィグ変更は、イベント化されたリプレイ可能な状態変化です（P6）。

### ブート耐性（Boot resilience）

`.reyn/` ディレクトリが存在しない、またはファイルが欠落している場合、そのコンポーネントには `{}` が返されます — no-op のリロードとなり、エラーにはなりません。リロードでセッションがクラッシュすることはありません。

## コンポーネントごとのリアプライシーム

5 つのシームがセッション構築時に `HotReloader` に登録されます。すべてのシームはリロードのたびに実行されます:

| シーム | 動作 |
|--------|------|
| `cron` | 存在するジョブを追加 / 置換（名前でべき等）。**リムーバルディフ**: `_runtime_cron_names` で追跡されていて再読み込みされた `.reyn/config/cron.yaml` にないジョブはスケジュール解除されます。スタートアップ（`reyn.yaml`）の cron ジョブは削除不可。 |
| `mcp` | 既存のターン境界リフレッシュチェーン経由で MCP サーバーを再プローブします。インメモリツールキャッシュが変更されたかどうかを報告します。 |
| `per_agent_capability` | `.reyn/agents/<name>/profile.yaml` を再読み込みし、セッションが所有する 3 つのホルダー（session / skill_runner / router_host）の `allowed_mcp` を更新します。 |
| `new_agent` | 確認用の no-op: エージェント検出はファイルシステムライブ（`AgentRegistry` は呼び出しごとに `.reyn/agents/` を走査）のため、新しいエージェントはリロードなしで既に可視です。明示的なシームとしてアカウンティングのために保持。 |
| `hooks` | グローバルの `.reyn/config/hooks.yaml` + ペーエージェントの `.reyn/agents/<name>/hooks.yaml` を再読み込みし、固定のスタートアップレイヤーと再結合し、フックディスパッチャーのレジストリを交換します。 |

## フック 3 レイヤー COMBINE

フックレジストリは 3 つのレイヤーから順番に加算的に構築されます:

| レイヤー | ファイル | セット | リロード時の動作 |
|---------|---------|------|----------------|
| **startup** | `reyn.yaml` | OUT-set | ブート時に 1 回キャプチャ; 再読み込みしない |
| **runtime** | `.reyn/config/hooks.yaml` | IN-set | リロードのたびに再読み込み |
| **per-agent** | `.reyn/agents/<name>/hooks.yaml` | IN-set | リロードのたびに再読み込み |

COMBINE は加算的です: `startup ∪ runtime ∪ per-agent`。削除されたフックは再構築されたレジストリに存在しない — 削除は再構築によって処理されます（明示的な削除ステップは不要）。

**レイヤーごとのブート耐性。** 信頼されたスタートアップレイヤー（`reyn.yaml`、オペレーター管理）は読み込まれる必要があります — 失敗はフェイルラウドです。各 untrusted レイヤー（runtime、per-agent）は独立して try-add されます:

- 不正な runtime レイヤーは `startup ∪ per-agent` を維持し、不正なレイヤーはドロップ + 警告されます。
- 不正な per-agent レイヤーは `startup ∪ runtime` を維持し、不正なレイヤーはドロップ + 警告されます。

リロードパスでは、validate-before-apply も不正な runtime レイヤーを事前に拒否します（多層防御）。

## トリガー

### オペレーター: `/reload`

`/reload` スラッシュコマンドは、次のターン境界でリロードをスケジュールします。

```
/reload
```

OUT-set（`reyn.yaml`）には触れません。リロードがスケジュールされ次のターン境界で適用されるという確認メッセージが返ります。

### エージェント自己リロード: `hooks_add`

`hooks_add` LLM-op は、`.reyn/config/hooks.yaml` にプッシュフックを書き込み、リロードをスケジュールします。フックは `hooks` リアプライシームを経由して次のターン境界で有効になります。

`hooks_add` パラメーター:

| パラメーター | 必須 | 説明 |
|------------|------|------|
| `on` | はい | ライフサイクルポイント: `turn_start`、`turn_end`、`session_start`、`session_end` |
| `message` | はい | プッシュメッセージ（Jinja2 テンプレート使用可能） |
| `wake` | いいえ | `true` → 新しいターンを開始（自己継続、`safety.loop.max_hook_driven_turns` で制限）; `false` → 次のターンのコンテキストとして乗る。デフォルト `true`。 |
| `push_when` | いいえ | Jinja2 → bool ガード; false にレンダリングされた場合にプッシュをスキップ。 |
| `name` | いいえ | 履歴の `[hook:name]` アトリビューションプレフィックスとして表示されるラベル。 |

ツールはライトゲートされています: 呼び出し側のスキルは `permissions.tool` に `hooks_add` を宣言する必要があり、ケイパビリティプロファイルの `tool_deny` でそれを拒否できます。

## セーフティストーリー

ホットリロードは 5 層の構造によって安全です:

1. **構造的ライトゲート。** `load_hot_reload_config` は `reyn.yaml` を開きません。`hooks_add` は書き込み先を `.reyn/config/hooks.yaml` にハードコードしています — パスは LLM の入力から導出されることはありません。LLM がトリガーするリロードは構造的に OUT-set に触れることができません。
2. **適用前バリデーション。** 不正な IN-set はリロード全体をアトミックに拒否します — ハーフアプライはなく、ライブコンフィグは変更されません。
3. **ブート耐性。** untrusted レイヤーのレイヤーごとの独立した try-add: 不正なレイヤーはブートのクラッシュや兄弟レイヤーのドロップなしにドロップ + 警告されます。
4. **サンドボックス + ループバルブ。** フックの `wake:true` ループは `safety.loop.max_hook_driven_turns` で制限されます。サンドボックスはシェルフック実行を保護します。
5. **ケイパビリティプロファイル拒否。** ケイパビリティプロファイルの `tool_deny: [hooks_add]` はエージェントがフックを追加することを防ぎます — ∩ モデルを通じてエージェントごとに機能を無効化できます。[ケイパビリティプロファイル](capability-profile.md)を参照してください。

## 参照

- [コンセプト: フック](hooks.md) — 6 つのライフサイクルポイント、push/shell スキーム、ウェイクループの動作
- [コンセプト: ケイパビリティプロファイル](capability-profile.md) — `hooks_add` の `tool_deny` ゲート; per-agent-capability リアプライシーム
- [コンセプト: パーミッションモデル](permission-model.md) — ∩ モデルとライトゲート境界
- [リファレンス: reyn.yaml § hooks](../../reference/config/reyn-yaml.md#hooks-block) — スタートアップフックコンフィグ（OUT-set）
- [リファレンス: イベント](../../reference/runtime/events.md) — `config_reloaded` P6 イベント
