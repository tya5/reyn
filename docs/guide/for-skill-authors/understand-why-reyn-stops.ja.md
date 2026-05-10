---
type: how-to
topic: operations
audience: [human]
---

# Reyn が止まる理由を理解する

Reyn が実行を中断するときは、必ず**次の 3 つのいずれか**が原因です:

1. **ループ検知** — 同じことを繰り返している
2. **タイムアウト** — 時間がかかりすぎている
3. **予算超過** — トークン / USD の上限に達した

各カテゴリは独自の設定名前空間を持ち、エラーメッセージには「このキーを上げれば
継続できる」というヒントが組み込まれています。本ページは、止まる理由と設定キーの
対応関係をまとめます。

> **TL;DR:** 統一された名前空間は、ループ / タイムアウトが `safety.*`、
> 財務的な上限は `cost.*` です。旧来のキー (`limits.*`, `multi_agent.*`) は
> 後方互換のためまだ動きますが、新規設定は `safety.*` を使ってください。

---

## ① ループ検知 — `safety.loop.*`

ループ検知は*暴走的な繰り返し*を捕まえる仕組みです。フェーズが永遠に再入する、
ルーターが何度もルーティングし直す、委譲チェーンが青天井に伸びる、など。
開発中に到達するのは正常です。本当に必要な反復数なら上限を引き上げ、
そうでないなら原因を調査してください。

| 上限 | 検出対象 | 既定値 | 設定キー |
|---|---|---|---|
| フェーズ訪問回数 | 1 スキル実行内で同じフェーズに入りすぎた | 25 | `safety.loop.max_phase_visits` |
| フェーズ内 act ターン数 | 1 フェーズ訪問内の LLM ↔ op の往復 | 10 | `safety.loop.max_act_turns_per_phase` (skill / phase frontmatter が優先) |
| 1 ターンあたりの router 呼び出し | 1 ユーザーターン内のルーター起動回数 | 3 | `safety.loop.max_router_calls_per_turn` (0 = 無制限) |
| エージェント委譲の深さ | `user → A → B → C` のチェーンが深すぎる | 3 | `safety.loop.max_agent_hops` |
| チェーン内スキル起動回数 | 同一スキルが同一チェーンで起動しすぎた | 無制限 | `safety.loop.max_skill_calls_per_chain` |

### エラーの例

```
Phase 'revise' reached max_phase_visits=25.
→ Raise safety.loop.max_phase_visits to allow more iterations.
```

### 修正の例

```yaml
# reyn.local.yaml
safety:
  loop:
    max_phase_visits: 50      # フェーズあたり 50 回まで許可
    max_router_calls_per_turn: 5
```

---

## ② タイムアウト — `safety.timeout.*`

タイムアウトは*時間がかかりすぎている*ものを捕まえます。LLM 呼び出しが遅い、
委譲先が応答しない、フェーズが 1 時間動き続けている、など。本当に時間が
必要なら上限を引き上げ、そうでないなら原因を調査してください。

| 上限 | 検出対象 | 既定値 | 設定キー |
|---|---|---|---|
| LLM 1 呼び出し | 1 回の litellm.acompletion がタイムアウトを超えた | 60 秒 | `safety.timeout.llm_call_seconds` |
| LLM リトライ | 一時エラー時のリトライ上限 | 3 | `safety.timeout.llm_max_retries` |
| フェーズ wall-clock | 1 フェーズ訪問が時間予算を超えた | 無制限 (`0`) | `safety.timeout.phase_seconds` |
| Chain 待機 | マルチエージェントの pending chain が委譲応答を待ちすぎた | 60 秒 | `safety.timeout.chain_seconds` (0 = タイムアウトなし) |

### エラーの例

```
chain timeout: 1 delegate(s) (writer) did not respond within 60s.
→ Raise safety.timeout.chain_seconds to wait longer (0 = no timeout).
```

### 修正の例

```yaml
# reyn.local.yaml
safety:
  timeout:
    llm_call_seconds: 120     # 遅いモデル用
    chain_seconds: 300        # 長時間動く委譲先用
```

---

## ③ 予算超過 — `cost.*`

予算上限は**財務的なキャップ**(トークン数、USD 額、日次 / 月次クォータ) です。
意図的に `cost:` 配下に残し、`safety:` には統合しません。運用者の感覚として、
ループ / タイムアウトは「上げるべき」ことが多いのに対し、予算は「調査するか
明示的に承認する」ことが多いためです。

| 上限 | 検出対象 | 設定キー |
|---|---|---|
| エージェントごとのトークン | 1 エージェントがトークン上限に達した | `cost.per_agent_tokens.hard_limit` |
| エージェントごとの USD | 1 エージェントが USD 上限に達した | `cost.per_agent_cost_usd.hard_limit` |
| (chain, skill) ごとの起動回数 | 同チェーン内で同スキルが起動しすぎた | `cost.per_chain_skill_calls.hard_limit` (`safety.loop.max_skill_calls_per_chain` でも可) |
| (chain, skill) ごとのトークン | 同チェーン内で同スキルがトークンを使いすぎた | `cost.per_chain_skill_tokens.hard_limit` |
| 日次クォータ | 当日の合計が `daily_tokens` / `daily_cost_usd` を超えた | `cost.daily_tokens.hard_limit`, `cost.daily_cost_usd.hard_limit` |
| 月次クォータ | 当月の合計が `monthly_tokens` / `monthly_cost_usd` を超えた | `cost.monthly_tokens.hard_limit`, `cost.monthly_cost_usd.hard_limit` |
| レートリミット | モデルごとの requests-per-minute 上限 | `cost.rate_limit_per_minute.<model>` |

### 上限到達時のユーザー承認フロー (FP-0003)

(chain, skill) ごとの起動回数キャップは、即時拒否ではなく*対話的な承認*に
切り替えできます:

```yaml
# reyn.local.yaml
cost:
  per_chain_skill_calls:
    hard_limit: 5
    ask_on_exceed: true       # ask_user 経由で問い合わせる
    extension_calls: 3        # 承認時に +3 回付与
```

上限到達時に Reyn が *「Skill `X` が上限 5 回に達しました。+3 回追加で
継続しますか?」* と問います。承認は何度でも可能で、毎回 `extension_calls`
分だけキャップが拡張されます。

---

## 旧設定キーからの移行

旧キーは現状動き続けます。統一された `safety:` への対応関係は次の通りです:

| 旧キー | 新キー |
|---|---|
| `limits.phase.max_visits` | `safety.loop.max_phase_visits` |
| `limits.phase.max_wall_seconds` | `safety.timeout.phase_seconds` |
| `limits.llm.timeout` | `safety.timeout.llm_call_seconds` |
| `limits.llm.max_retries` | `safety.timeout.llm_max_retries` |
| `multi_agent.max_hop_depth` | `safety.loop.max_agent_hops` |
| `multi_agent.chain_timeout_seconds` | `safety.timeout.chain_seconds` |
| `cost.router_invocations_per_turn` | `safety.loop.max_router_calls_per_turn` |
| `cost.per_chain_skill_calls.hard_limit` | `safety.loop.max_skill_calls_per_chain` (`ask_on_exceed` のために `cost.*` 配下にも残置) |

新旧キーの両方が設定されているときは**新キーが優先**されます。旧キーは将来の
メジャーバージョンで削除予定です。今のうちに移行しておけば設定が
forward-compatible になります。

---

## limit 到達時の挙動 (`safety.on_limit`)

既定の挙動: limit 到達は実行を abort します。Reyn は `RunResult` を返し、
`status` を `loop_limit_exceeded` / `phase_budget_exceeded` /
`budget_exceeded` のいずれかに設定し、`partial_data` には最後に完了した
フェーズの artifact (= *「今ここまでの成果物」*) を入れます。

挙動は `safety.on_limit.mode` で変更できます:

```yaml
# reyn.local.yaml
safety:
  on_limit:
    mode: unattended       # 既定 — 到達時に abort (旧来の挙動)
    # mode: interactive    # ask_user で user に確認、 承認時に limit 延長して継続
    # mode: auto_extend    # N 回まで自動延長、 それ以降は abort
    auto_extend_times: 1   # auto_extend 時のみ参照
    ask_timeout_seconds: 60  # interactive 時のみ参照
```

| モード | 用途 |
|---|---|
| `unattended` (既定) | `reyn run` / CI / スクリプト実行 — 確認できる人がいない、 fail fast 推奨 |
| `interactive` | `reyn chat` / TUI session — ユーザーが目の前にいて判断できる |
| `auto_extend` | 信頼済みの長時間タスクで「N 回までは自動延長して良い」と分かっているとき |

> **Phase 1 ステータス (FP-0005):** 設定スキーマと `RunResult.partial_data`
> フィールドは landing 済みです。limit ごとの `ask_user` dispatch (=
> `interactive` / `auto_extend` が実際に到達時に対話に切り替わる) は段階的
> rollout で、 詳細は [FP-0005
> proposal](../../deep-dives/proposals/0005-safety-as-checkpoint.md) の
> per-limit migration plan を参照してください。各 site の wiring が
> landing するまでは、 当該 limit に対して `mode: interactive` を設定しても
> `unattended` (= 旧来の abort) にフォールバックします。
>
> 唯一の例外: `cost.per_chain_skill_calls.ask_on_exceed: true` (FP-0003)
> は (chain, skill) ごとの起動回数キャップに対するユーザー承認フローを
> 既に提供しています — 上記 §③ の例を参照してください。
