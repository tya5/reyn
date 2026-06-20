---
type: reference
topic: config
audience: [human, agent]
applies_to: [reyn.yaml]
---

# Budget とコスト追跡

## 概要

Reyn は LLM のトークン使用量と USD コストを、セッション単位・agent 単位・chain 単位・model 単位で追跡します。トークンと USD の累計は LLM 呼び出しが完了するたびに更新され、設定された cap を超えそうな呼び出し（またはスポーン）は事前に拒否またはワーニングされます。このシステムは完全に opt-in です。`reyn.yaml` に `cost:` ブロックがなければ、実行回数・コストは無制限になります。

## `reyn.yaml` スキーマ

すべての budget 設定はトップレベルの `cost:` キー以下に記述します。各フィールドはすべて任意です。サブキーを省略するか `hard_limit` を `null` にした場合、その dimension は無制限になります。

```yaml
cost:
  # Per-agent caps — ledger でバック（再起動・クラッシュをまたいで保持）。
  # /budget reset で in-memory をクリア
  per_agent_tokens:
    hard_limit: 50000    # 1 agent がこのトークン数を超えたら拒否
    warn_ratio: 0.8      # hard_limit の 80% でワーニング（デフォルト: 0.8）
  per_agent_cost_usd:
    hard_limit: 2.00     # 1 agent が $2.00 を超えたら拒否
    warn_ratio: 0.8

  # Per-model rate limit（hard cap、60 秒ウィンドウあたりの呼び出し数）
  rate_limit_per_minute:
    openai/gpt-4o: 60
  rate_limit_warn_ratio: 0.8   # rate limit の 80% でワーニング（デフォルト: 0.8）

  # Daily / monthly quota — プロセス再起動をまたいで永続化（PR25）
  # .reyn/state/budget_ledger.jsonl に保存。ローカル時刻の日付境界 / 月初に自動リセット。
  daily_tokens:
    hard_limit: 100000   # 本日 100k トークンを超えたら拒否
    warn_ratio: 0.8
  daily_cost_usd:
    hard_limit: 5.00     # 本日 $5.00 を超えたら拒否
  monthly_tokens:
    hard_limit: 1000000  # 今月 1M トークンを超えたら拒否
  monthly_cost_usd:
    hard_limit: 50.00    # 今月 $50.00 を超えたら拒否
```

> **移行案内**: `per_chain_skill_calls`、`per_chain_skill_tokens`、`router_invocations_per_turn` は `cost:` から `safety.loop` に移動しました。代わりに `safety.loop.skill_calls_per_chain`、`safety.loop.skill_tokens_per_chain`、`safety.loop.max_router_calls_per_turn` を使用してください。[リファレンス: `reyn.yaml` — `safety` ブロック](reyn-yaml.ja.md#safety-ブロック) を参照。

### フィールドリファレンス

| フィールド | スコープ | 永続化 | リセットタイミング |
|---|---|---|---|
| `per_agent_tokens` | per agent | ledger ファイル | `/budget reset` |
| `per_agent_cost_usd` | per agent | ledger ファイル | `/budget reset` |
| `rate_limit_per_minute` | per model | in-memory (60s ウィンドウ) | 自動スライディングウィンドウ |
| `rate_limit_warn_ratio` | グローバル | — | — |
| `daily_tokens` | process-global | ledger ファイル | ローカル時刻の深夜 |
| `daily_cost_usd` | process-global | ledger ファイル | ローカル時刻の深夜 |
| `monthly_tokens` | process-global | ledger ファイル | 月初 1 日（ローカル時刻） |
| `monthly_cost_usd` | process-global | ledger ファイル | 月初 1 日（ローカル時刻） |

各 cap dimension には以下のサブフィールドがあります。

| サブフィールド | 型 | デフォルト | 説明 |
|---|---|---|---|
| `hard_limit` | float または null | null（無制限） | この値に達したか超えた場合、次の LLM 呼び出しまたはスポーンを拒否する。 |
| `warn_ratio` | float | 0.8 | `hard_limit * warn_ratio` に達したときにワーニングを発行する。ワーニングは 1 セッションあたり 1 dimension につき 1 回のみ。 |

### USD コスト計算

USD コストは各呼び出し後に [LiteLLM の pricing lookup](https://github.com/BerriAI/litellm) で推定されます。proxy モード（LiteLLM 経由）と直接 API の両方に対応しています。対象 model の価格情報が見つからない場合、USD カウンターは `$0.0000` のままとなり、トークンのみが累積されます。トークン数は価格情報の有無にかかわらず常に正確に記録されます。

## スラッシュコマンド

`reyn chat` セッション中に、以下の 2 つのスラッシュコマンドで budget 状態を確認できます。

### `/cost`

現在アタッチしている agent の 1 行サマリー：

```
/cost
```

出力例：

```
alice: 12,450 tokens, $0.0187  (this session)
```

この agent の per-agent カウンターを表示します。これらは起動時に ledger から復元され（再起動をまたいで累積）、`/budget reset` で in-memory がクリアされます。`cost:` ブロックが設定されていない場合（無制限モード）は何も返りません。

### `/budget`

このセッションで確認できたすべての dimension と agent の全体ビュー：

```
/budget
```

出力例：

```
Usage (process invocation):

  Today (2026-05-09):   tokens 12,450 / 100,000 (12%) | $0.0187 / $5.00 (0%)
  Month (2026-05):      tokens 12,450 / 1,000,000 (1%) | $0.0187 / $50.00 (0%)

  alice (attached)
    tokens:       12,450 / 50,000  (warn at 40,000)
    cost:         $0.0187 / $2.00     (warn at $1.60)

  Per-chain skill calls:
    chain-abc/direct_llm:  2 / 5

  Rate limit (last minute):
    openai/gpt-4o:  14 / 60  (warn at 48)

  Reset counters with `/budget reset`.
```

「Today / Month」セクションは、`daily_*` または `monthly_*` cap が設定されており、起動後に少なくとも 1 回 LLM 呼び出しが行われた場合にのみ表示されます。

### `/budget reset`

in-memory の per-agent・per-chain カウンターをクリアします：

```
/budget reset
```

Daily / monthly カウンターはリセット対象外です。これらは永続化された ledger（`.reyn/state/budget_ledger.jsonl`）が管理しており、期間境界で自動リセットされます。手動でクリアするにはプロセスを停止した状態で ledger ファイルを削除または退避してください。

## Cap の 2 段階動作

各 dimension には 2 段階のしきい値があります。

**Soft warn（ワーニング）** — 使用量が `hard_limit * warn_ratio` に達した時点で 1 回だけ発行されます。LLM 呼び出しは続行され、`[budget warn]` ステータスメッセージがユーザーに表示され、event log に記録されます。

**Hard refuse（拒否）** — 使用量が `hard_limit` に達するか超えた場合に発動します。LLM 呼び出しは実行前に拒否されます（トークンは消費されません）。`[budget exceeded]` メッセージが表示され、現在の使用量・トリガーされた dimension・3 つの回復手順が示されます。

```
[budget exceeded] agent 'alice' is over the hard limit.

  Triggered:  per_agent_tokens (50,123/50,000)
  Also used:  $0.0374

The next LLM call has been refused.

What you can do:
  • Raise the limit in `reyn.yaml` or `reyn.local.yaml` (cost: section)
  • Reset counters with `/budget reset`
  • Restart `reyn chat` (limits are per-process)
  • See current usage with `/budget`
```

rate limit 違反（`rate_limit_per_minute`）の場合、60 秒ウィンドウ内の次の呼び出しが枠内に収まるまで拒否され続けます（自動スリープ / throttle はなく、呼び出し側がリトライする必要があります）。

## 発行されるイベント

| イベント | 発行タイミング |
|---|---|
| `router_retry_exhausted` | `safety.loop.max_router_calls_per_turn` の cap に達したとき。`count`・`cap`・`last_reason` を保持 |
| `budget_reset` | `/budget reset` 実行時。リセット前のカウンタースナップショットを `before` に保持 |

ワーニングと拒否のシグナルは、独立したイベント型ではなく outbox メッセージとしてユーザーに届きます。`BudgetCheck` の戻り値を runtime が検査し、適切なメッセージを構築します。

参照: [reference/runtime/events.md](../runtime/events.md)

## Per-call 蓄積の仕組み

LLM 呼び出しが正常完了するたびに以下の順で処理されます。

1. トークン使用量（`input_tokens + output_tokens`）が per-agent・per-chain のアキュムレーターに加算されます。
2. LiteLLM pricing による USD コスト推定値が USD アキュムレーターに加算されます。
3. `.reyn/state/budget_ledger.jsonl` に 1 レコードが追記されます（fsync 済み、耐久性あり）。daily / monthly / per-agent カウンターは次回起動時にこのレコード群から再構築されます。
4. 更新後のカウンターが warn しきい値と比較され、新たに超えた dimension があれば outbox ワーニングメッセージが 1 回だけ発行されます。

Pre-call チェックは呼び出し前に実行されます。すでに hard cap を超えている場合、その時点で呼び出しが拒否されトークンは消費されません。

## Ledger ファイル

budget カウンターは fsync-per-append の `.reyn/state/budget_ledger.jsonl` によってプロセス再起動（およびクラッシュ）をまたいで永続化されます。オプションの `kind` フィールドで区別される 2 種類のレコードを保持します。

1 LLM 呼び出し = 1 レコード（`kind` なし）：

```json
{"ts": "2026-05-09T10:23:00+09:00", "agent": "alice", "model": "openai/gpt-4o", "tokens": 312, "cost_usd": 0.00234}
```

1 skill spawn = 1 レコード（`kind: "spawn"`）。per-chain spawn count cap を耐久的にバックします：

```json
{"ts": "2026-05-09T10:23:01+09:00", "kind": "spawn", "chain_id": "ab12…", "skill": "eval"}
```

レコードは追記のみ（append-only）で fsync されます。起動時に Reyn は ledger から次を再集計します：本日・今月の daily / monthly 合計（期間フィルタ済み）、累積の per-agent token + USD 合計、per-chain skill spawn count。ledger が cap の信頼源（source of truth）であり、`.reyn/state/budget_state.json` はその上に重ねた throttle 付きベストエフォートのキャッシュです（ledger に対して最大 1 秒遅れることがあるため、復旧時は ledger の値が常に優先されます）。ファイルは月数 MB 程度ずつ増加します。手動でアーカイブする場合はプロセスを停止してから行うか、期間ロールオーバーを待ってください。

## Per-agent・per-chain cap の復旧セマンティクス

`per_agent_tokens`・`per_agent_cost_usd` および per-chain skill スポーン cap は
**ライフタイム永続**です。起動のたびに all-time の durable ledger から再構築され、
クラッシュや再起動をまたいでも値が失われません。

**会話（conversation）ごとにリセットされるわけではありません。** カウンターは継続的に
累積され、`/budget reset`（in-memory クリア）または ledger ファイルのアーカイブによって
のみクリアされます。

daily / monthly cap との対比：こちらは期間境界（ローカル時刻の深夜または月初 1 日）で
自動リセットされます。プロセスの再起動・クラッシュにかかわらず、境界を越えれば必ずリセットされます。

**クラッシュリカバリ保証**: クラッシュによって per-agent・per-chain のカウンターが
durable ledger の値を下回ることはありません。復旧時に `load_state`（throttle 付きベストエフォートキャッシュ）は
`hydrate`（ledger）と `max()` でマージされます。そのため、古いまたはガベージが混入した
state ファイルによって cap のカウントが減少し、予算超過の呼び出しが許可されることはありません。
設計の根拠：クラッシュリカバリは完全性が必要であり、クラッシュによってライフタイム cap が
リセットされてしまうと、人間が気づくまでの間に無制限の過剰消費が発生しうるためです。

## 未実装の機能

以下の制限事項があります。

- **Auto-throttle** — rate limit に達した場合、Reyn はウィンドウが空くまでスリープするのではなく呼び出しを拒否します。呼び出し側がリトライする必要があります。
- **Cross-process / マルチテナント budget** — `reyn chat` や `reyn web` の各プロセスは独立した in-memory カウンターを持ち、他の稼働中プロセスの ledger レコードを取り込むのは次回起動（hydrate）時のみです。したがって同時稼働するプロセスはすべての cap をリアルタイムに独立適用し、共有 ledger が daily / monthly / per-agent の合計と per-chain spawn count を整合させるのはプロセス再起動時に限られます。

## 関連ドキュメント

- [reference/runtime/events.md](../runtime/events.md) — イベント全カタログ
- [reference/cli/chat.md](../cli/chat.md) — `/cost`・`/budget` などのスラッシュコマンド
- [reference/config/reyn-yaml.md](reyn-yaml.md) — トップレベル設定スキーマ（`cost:` ブロックはこちらでも参照可能）
