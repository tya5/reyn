---
type: how-to
topic: using-reyn
audience: [human]
---

# 支出に上限をかける

デフォルトでは Reyn は支出制限なしで動作します — タスクが終わるまで LLM を
呼び続けます。トークン数や金額にハードな上限を設けたい場合は、`reyn.yaml` に
`cost:` ブロックを追加します。上限は各 LLM 呼び出しの**前**にチェックされるため、
拒否された呼び出しには費用がかかりません。

## 1日あたりのドル上限を設定する

最もよくある目的 ——「1日に数ドル以上は使わない」:

```yaml
# reyn.yaml
cost:
  daily_cost_usd:
    hard_limit: 5.00     # 当日の支出が $5 に達したら次の呼び出しを拒否
    warn_ratio: 0.8      # 80%（$4.00）で一度だけ警告
```

日次・月次の上限は**永続的**です — 再起動しても保持され、ローカル時刻の
深夜0時（日次）または月初1日（月次）に自動リセットされます。
`.reyn/state/budget_ledger.jsonl` に保存されます。

## 単一エージェント/セッションに上限をかける

エージェント単位の上限は**メモリ内**です — `reyn chat` の再起動や
`/budget reset` でリセットされます。1日全体ではなく、1つの会話を
バウンドしたいときに使います:

```yaml
cost:
  per_agent_tokens:
    hard_limit: 50000
  per_agent_cost_usd:
    hard_limit: 2.00
```

## 現在の状況を確認する

`reyn chat` 実行中:

| コマンド | 表示内容 |
|---------|---------|
| `/cost` | アタッチ中エージェントの当セッション支出（1行） |
| `/budget` | 全体内訳 — 当日・当月・エージェント別・レート制限 |
| `/budget reset` | メモリ内のエージェント別カウンタをクリア（日次/月次は不変） |

## 上限に達したときの挙動

- **警告しきい値**（`hard_limit × warn_ratio`）: 一度だけ `[budget warn]`
  メッセージが出て、呼び出しは続行されます。
- **ハード上限**: 次の呼び出しは実行前に拒否されます。`[budget exceeded]` と
  発火した dimension、対処法（`reyn.yaml` で上限引き上げ / `/budget reset` /
  再起動）が表示されます。

## 補足

- `cost:` ブロックが無ければ実行は無制限 — フレームワーク全体が opt-in です。
- ドル金額は [LiteLLM の価格データ](https://github.com/BerriAI/litellm) から
  推定されます。価格エントリの無いモデルではドルカウンタは `$0.00` のままですが、
  トークン数は常に正確なので、価格不明でもトークン上限は機能します。
- レート制限（`rate_limit_per_minute`）は60秒ウィンドウが空くまで呼び出しを
  拒否します。Reyn は自動スリープしないため、呼び出し側がリトライします。

## 関連

- [リファレンス: Budget and cost tracking](../../reference/config/budget.md) — 全スキーマ・全フィールド・ledger 形式・`/cost` / `/budget` の出力
- [リファレンス: `reyn.yaml`](../../reference/config/reyn-yaml.md) — 設定ファイル全体
- [Reyn が停止する理由を理解する](../for-skill-authors/operations/understand-why-reyn-stops.md) — limit と budget は1つの停止フレームワークを共有
