# FP-0004: safety 設定 UX 改善 — 概念レイヤーとの整合

**Status**: done (landed 2026-05-10、 commit `414f87a`)
**Proposed**: 2026-05-10
**Author**: Research session (eager-shaw-389d9d)
**Implemented**: 2026-05-10 — `SafetyConfig` + `LoopConfig` + `TimeoutConfig` データクラス (`src/reyn/config.py`) + `hint_config_key` を `LoopLimitExceededError` / `PhaseBudgetExceededError` / `RouterCapExceeded` に追加 + chain-timeout / max-hop-depth エラーメッセージのヒント + `docs/guide/for-skill-authors/understand-why-reyn-stops.md` (en+ja) + 11 Tier 2 invariants (`tests/test_safety_config.py`)。 当初は legacy `limits:` / `multi_agent:` / `cost.router_invocations_per_turn` / `cost.per_chain_skill_calls.hard_limit` キーを back-fill する deprecation reader 込みで landing したが、 `0b464ab` で legacy 互換層を撤去し `safety:` を single source of truth に確定 (= 移行パスなし、 `0.x` ユーザの `reyn.yaml` は手動 migration 必須)。

---

## Summary

現在のループ対策・タイムアウト設定は `limits:` / `cost:` / `multi_agent:` の
3 セクションに分散しており、利用者が「なぜ止まったか」「何を変えれば再開できるか」を
理解しにくい。ユーザーの認知モデル（止まる理由は ループ / タイムアウト / 予算超過 の 3 種類）
に合わせて `safety:` セクションへ統合し、エラーメッセージとドキュメントも揃える。

---

## Motivation

### 現状の問題

**設定キーが 3 名前空間に分散:**

```yaml
limits:
  phase:  max_visits / max_wall_seconds
  llm:    timeout / max_retries
cost:
  router_invocations_per_turn
  per_chain_skill_calls          # ← 役割は「ループ検知」だが cost: に置かれている
multi_agent:
  max_hop_depth / chain_timeout_seconds
# + skill.md frontmatter の max_act_turns
```

**エラーメッセージに設定キーが含まれない:**

```
[loop_limit_exceeded]
→ どのキーを変えれば続くのかユーザーには分からない
```

**「止まる理由」の概念モデルがドキュメントにない:**

10 機構が個別に説明されており、利用者がメンタルモデルを構築できない。

---

## Proposed implementation

### Step 1 — エラーメッセージに設定キーを含める（SMALL）

```
[ループ検知] phase 'revise' が 25 回の上限に達しました。
→ 続けるには safety.loop.max_phase_visits を引き上げてください。

[タイムアウト] LLM 呼び出しが 60 秒を超えました。
→ safety.timeout.llm_call_seconds を引き上げるか、モデルを変更してください。

[ループ検知] router がこのターンに 3 回起動されました。
→ safety.loop.max_router_calls_per_turn を引き上げてください（0 = 無制限）。
```

各 raise / return 箇所で `hint_config_key` を付与するだけ。既存の挙動は変わらない。

### Step 2 — `safety:` セクションへ統合（MEDIUM）

概念レイヤーに対応した新設定スキーマ:

```yaml
safety:

  # ① ループ検知 — 同じことを繰り返しているとき
  loop:
    max_act_turns_per_phase: 10    # フェーズ内（LLM と op のやりとり）
    max_phase_visits: 25           # フェーズ間（遷移の繰り返し）
    max_router_calls_per_turn: 3   # スキル起動（同一ターン内）
    max_agent_hops: 3              # 委譲連鎖（A→B→C の深さ）
    max_skill_calls_per_chain: 5   # スキル起動（チェーン全体）

  # ② タイムアウト — 時間がかかりすぎているとき
  timeout:
    llm_call_seconds: 60           # LLM API 1 回の呼び出し
    llm_max_retries: 3             # LLM 一時エラーのリトライ上限
    phase_seconds: 0               # フェーズ全体（0 = 無制限）
    chain_seconds: 60              # マルチエージェント chain

# ③ 予算超過 — cost: セクションのまま維持
# （トークン / USD / 日次 / 月次 は財務的な設定として分離を保つ）
cost:
  per_agent_tokens: ...
  daily_cost_usd: ...
  ...
```

**移行方針（後方互換）:**

- 旧キー（`limits.phase.max_visits` 等）は deprecated として読み取りを継続
- 新キーが存在する場合は新キーを優先
- 次のメジャーバージョンで旧キーを削除

**`per_chain_skill_calls` の移動:**

現在 `cost:` セクションにある `per_chain_skill_calls` は役割が「ループ検知（回数制限）」であるため
`safety.loop.max_skill_calls_per_chain` として移動。財務的な意味を持たないため `cost:` への残留は誤誘導。

**`max_act_turns` の扱い:**

現在はフェーズ frontmatter に `max_act_turns: 10` と書く。
グローバルデフォルトを `safety.loop.max_act_turns_per_phase` として追加し、
フェーズ frontmatter でのオーバーライドは引き続き可能。

### Step 3 — 概念ドキュメント作成（SMALL）

`docs/guide/for-skill-authors/` または `docs/guide/for-reyn-developers/` に
`understand-why-reyn-stops.md` を追加:

```
# なぜ Reyn は止まるか

止まる理由は 3 種類:
  ① ループ検知 → safety.loop.*
  ② タイムアウト → safety.timeout.*
  ③ 予算超過 → cost.*

各カテゴリごとに:
  - 何が起きているか（例）
  - 該当するエラーメッセージ
  - 変更すべき設定キー
  - 推奨値と注意点
```

---

## Dependencies

- `src/reyn/config.py` — `SafetyConfig` + `LoopConfig` + `TimeoutConfig` データクラス追加
- `src/reyn/kernel/runtime.py` — エラーメッセージに `hint_config_key` 付与
- `src/reyn/chat/session.py` — router cap / chain エラーのメッセージ改善
- `src/reyn/chat/services/chain_manager.py` — chain timeout エラーメッセージ改善
- `src/reyn/chat/services/budget_gateway.py` — `per_chain_skill_calls` を新キーに移行
- `docs/reference/config/reyn-yaml.md` — 設定リファレンス更新

前提 PR: なし（独立して実装可能。Step 1 → 2 → 3 の順に独立してリリース可能）

---

## Cost estimate

**合計: MEDIUM**

| タスク | コスト | 備考 |
|---|---|---|
| Step 1: エラーメッセージ改善 | SMALL | 各 raise 箇所に文字列追加のみ |
| Step 2: `SafetyConfig` データクラス定義 | SMALL | config.py に型追加 |
| Step 2: 旧キー → 新キー の移行レイヤー | SMALL | deprecated 読み取りロジック |
| Step 2: 各設定の参照箇所を新キーに変更 | MEDIUM | runtime / session / chain_manager 等 複数ファイル |
| Step 3: 概念ドキュメント | SMALL | 新規 .md 1 ファイル |

ボトルネックは **Step 2 の参照箇所変更**（複数モジュールに散在）。

---

## Related

- `src/reyn/config.py` — 現行の `CostConfig` / `LimitsConfig` / `MultiAgentConfig`
- `src/reyn/kernel/runtime.py` — `LoopLimitExceededError`, `PhaseBudgetExceededError`
- `src/reyn/chat/services/budget_gateway.py` — `RouterCapExceeded`
- FP-0003 (`0003-budget-exceed-user-approval.md`) — budget 超過時の ask_user 連携（`safety.loop.max_skill_calls_per_chain` も対象になる）
