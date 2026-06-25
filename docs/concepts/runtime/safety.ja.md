# 安全フレームワーク — 制限・モード・介入フロー

Reyn の安全フレームワークは、agent がどれだけ長く実行できるか、どれだけ深く再帰できるか、システムが停止するまでに何回ループできるかを制限します。境界を超えるすべての操作は単一のチェックポイント API（`handle_limit_exceeded`）を共有するため、オペレーターは一度動作を設定すれば均一に適用されます。

設計原則：**制限はオペレーターへの問い合わせ（interactive）かクリーンな部分/デグレードなしでハードストップしない**——すべてのチェックポイントは、継続許可をオペレーターに求めるか、設定済みバジェット内で自動拡張するか、何を変更すべきかを説明するメッセージとともに停止します。

**並列ゲート設計.** [パーミッションシステム](permission-model.md)（tier-2/3 ops の JIT オペレーター問い合わせ）とこの制限フレームワークは意図的に並列です：両者は対話型パスに `RequestBus` インターフェースを共有し、同じ 3 状態ゲート（`allow` / `deny` / ask）を提供し、バスが配線されていない場合は同様に deny にデグレードします。一方のゲートを理解したオペレーターはもう一方も理解できます。

---

## モード（`safety.on_limit.mode`）

| mode | 制限到達時の動作 |
|---|---|
| `interactive`（デフォルト） | 介入バス経由でオペレーターに yes/no を問い合わせ。yes で継続、no / タイムアウトで中断。 |
| `auto_extend` | `(run_id, limit_kind)` ごとに `safety.on_limit.auto_extend_times` 回まで自動拡張し、その後中断。 |
| `unattended` | 即座に中断。問い合わせなし。 |

介入バスが利用できない場合（ヘッドレス / 非 TTY 実行）、`interactive` は `unattended` と同じ中断パスにデグレードします。すべての中断パスで、アウトボックスのエラーメッセージは**判断を促す**内容になります：どの制限に到達したか、現在の設定値、変更すべき設定キーを示します。

---

## 制限インベントリ

| 制限 | 設定パス | デフォルト | チェックポイント？ | 部分データ？ |
|---|---|---|---|---|
| フェーズ act ターン数 | `safety.loop.max_act_turns_per_phase` | 10 | ✅ | あり |
| フェーズ訪問回数 | `safety.loop.max_phase_visits` | 25 | ✅ | あり |
| ルーター呼び出し / ターン | `safety.loop.max_router_calls_per_turn` | 3 | ✅ | あり |
| スキル呼び出し / チェーン | _（スキルごとに設定）_ | — | ✅ | あり |
| Agent ホップ数 | `safety.loop.max_agent_hops` | 3 | ✅ | あり |
| フェーズウォールクロック | `safety.timeout.phase_seconds` | 0（無効） | ✅ | あり |
| チェーン待機 | `safety.timeout.chain_seconds` | 60 | ✅ | あり |
| ルーターイテレーション | `safety.loop.max_router_iterations` | 5 | ✅ | 部分あり |
| LLM 呼び出しタイムアウト | `safety.timeout.llm_call_seconds` | 60 | ❌ 自動リトライ / 中断 | — |
| メディアキャップ | `multimodal.max_bytes` | 5 MB | ❌ 自動デグレード | — |
| サマリー本文キャップ | `chat.compaction.body_token_cap` | 1500 | ❌ 自動切り詰め | — |

✅ の行は `handle_limit_exceeded` を経由します。
❌ の行はオペレーター入力を必要としない自律的な動作を持ちます。

---

## 介入フロー

```
制限到達
  │
  ├─ mode=unattended  ──► allow=False, reason="unattended"
  │
  ├─ mode=auto_extend ──► バジェット内    → allow=True,  reason="auto_extended"
  │                       バジェット枯渇 → allow=False, reason="unattended"
  │
  └─ mode=interactive
        ├─ bus=None   ──► allow=False, reason="no_bus"
        └─ bus あり   ──► UserIntervention をディスパッチ
              ├─ yes  ──► allow=True,  reason="user_approved"
              └─ no   ──► allow=False, reason="user_refused"

すべての allow=False パス ──► force-close ラップアップ
      `limit_denied` イベント発火（kind = max_iterations | router_cap）
      → 達成内容をまとめる最後のツールなし LLM ターン 1 回
          ├─ ラップアップにテキストあり ──► outbox kind="agent",
          │                                 meta.limit_stopped=True, meta.limit_kind=<kind>
          └─ ラップアップ失敗 / 空        ──► 判断を促す outbox エラー（フォールバック）
```

**A2A ピアセッション.** A2A セッションは CLI セッションと同じ `on_limit` 設定を使います（デフォルト：`interactive`）。`interactive` モードで制限が発火すると、介入は `A2AInterventionBus` 経由で A2A ピアに通知されます。ランのステータスが `"input-required"` にミラーリングされ、ペイロードが SSE ストリーム / webhook に追記されます。ピアは A2A answer エンドポイント（`POST /a2a/agents/<name>` `{task_id, answer}`）で返答し、介入が解消されてループが継続します。ピア回答を無制限に待つのではなく、制限された動作を望む場合は `safety.on_limit.ask_timeout_seconds` に有限値（例：`ask_timeout_seconds: 60.0`）を設定してください——タイムアウトによる拒否は "no" 回答と同じ判断を促すエラーを生成します。

**deny 時の force-close ラップアップ.** 制限が拒否されても即座に定型エラーに移行しなくなりました。OS はまず `limit_denied` イベントを発火（監査真実、P6）し、ターンが終了する前に LLM に達成内容をまとめる最後の**ツールなし**ターンを 1 回与えます。停止原因はそのラップアップのシステムプロンプトに注入されます（定常状態の SP は原因中立のまま。一部のプロバイダーは `tool_result` の直後のユーザーターンを拒否するため、末尾のユーザーメッセージとしては追記しません）。ラップアップがテキストを生成した場合、構造化された `meta.limit_stopped=True` + `meta.limit_kind` マーカーを持つ通常の `kind="agent"` outbox メッセージとして配信されます。UI はマーカーを読み取って強制停止を表示します（競合する prose ブロックなし）。フェーズ / プランホストでは、ラップアップはチェックポイント永続化のために返されます（`record_force_close`）。chat ホストはこのフックを no-op 処理します。

**判断を促すエラーメッセージコントラクト** — **フォールバック**パス（ラップアップ呼び出しが例外を発生させたかテキストを生成しなかった場合）でのみ発火します。すべての `allow=False` パスは以下を含むメッセージにデグレードします：
1. どの制限に到達したか、現在の設定値
2. 増やすべき設定キー、または interactive / auto-extend 動作のための `safety.on_limit.mode` の設定方法
3. 部分的な結果が利用可能かどうか

---

## 設定リファレンス（`reyn.yaml`）

```yaml
safety:
  on_limit:
    mode: interactive          # interactive | auto_extend | unattended
    auto_extend_times: 1       # auto_extend モードで (run_id, limit_kind) ごとに付与する拡張回数
    ask_timeout_seconds: 0.0   # 0 = 永久に待機; >0 = タイムアウト後に拒否
  loop:
    max_act_turns_per_phase: 10
    max_phase_visits: 25
    max_router_calls_per_turn: 3
    max_agent_hops: 3
    max_router_iterations: 5   # ユーザーターンあたりの LLM ツール呼び出しイテレーション最大数（CLI --max-iterations で上書き可）
  timeout:
    phase_seconds: 0.0         # 0 = 無効
    chain_seconds: 60.0
    llm_call_seconds: 60.0
```
