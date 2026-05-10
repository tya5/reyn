# FP-0005: safety limit をチェックポイントとして扱う — Permission モデルとの統合

**Status**: done (= Phase 1 + Phase 2 landed 2026-05-10)
**Proposed**: 2026-05-10
**Author**: Research session (eager-shaw-389d9d)
**Phase 1 implemented**: 2026-05-10 (commit `8715a0b`) — `OnLimitConfig` (`mode` / `auto_extend_times` / `ask_timeout_seconds`) を `safety:` セクションに追加; `RunResult.partial_data` フィールド追加; `OSRuntime.run()` の abort パスが `loop_limit_exceeded` / `phase_budget_exceeded` / `budget_exceeded` で `partial_data` を populate。 **既定 mode = `unattended`** で legacy abort 即座挙動を byte-for-byte 維持; `interactive` / `auto_extend` は明示 opt-in。 8 Tier 2 invariants (`tests/test_safety_on_limit.py`)。
**Phase 2 implemented**: 2026-05-10 (commit `dad53f4`) — Shared `handle_limit_exceeded` helper (`src/reyn/safety/limit_handler.py`) + `LimitDecision` データクラス; 6 abort path 全 site の wiring (B max_phase_visits / F phase_seconds / A max_act_turns を OSRuntime; C router_cap / E max_hop_depth / G chain_seconds を ChatSession); FP-0003 の `_ask_budget_extension` (D per_chain_skill_calls) を shared helper 経由に generalize; CLI factories (chat / web / mcp) が `config.safety.on_limit` を ChatSession に thread。 11 helper invariants (`tests/test_safety_limit_handler.py`) + 6 wiring invariants (`tests/test_safety_phase2_wiring.py`)。 `_chains.get(chain_id)` peek-before-pop パターンで chain_seconds watchdog がユーザ承認時に pending entry を失わずに re-arm。

---

## Summary

現在の safety limit（ループ検知・タイムアウト・予算超過）は全て「abort = 成果物消失」として
実装されている。WAL がすでに状態を保全するインフラを持ち、Permission モデルが
「pause → ask → resume/abort」パターンを持つ。両者を統合することで、
limit 到達を「クラッシュ」ではなく「チェックポイント」として扱い、
ユーザーが成果物を失わずに継続判断できる設計にする。

---

## Motivation

### ユーザーの本質的なニーズ

```
今の挙動: limit 到達 → abort → それまでの LLM コスト・成果物が消える
欲しい挙動: limit 到達 → 通知 → ここまでの成果物は手元にある
                          → 続けるか止めるかをユーザーが決める
```

設定の複雑さを事前に理解させるよりも、**動かしてみて引っかかったら対話する**
というモデルの方がユーザー体験として自然。

### WAL はすでにインフラを持っている

H（LLM タイムアウト）が唯一再開できるのは、WAL にフェーズ状態が保存されるから。
他の limit でも「abort 前に WAL を確定させる」だけで成果物の保全は実現できる。
現状は abort 時に WAL 確定が保証されていないケースがある。

### Permission モデルとの対称性

```
ファイル書き込み権限なし → ask_user → 承認 → 続行
MCP ツール権限なし      → ask_user → 承認 → 続行
↓ 同じパターンで
loop limit 到達         → ask_user → 承認 → limit 延長して続行
timeout limit 到達      → ask_user → 承認 → deadline 延長して続行
```

---

## Proposed implementation

### コアの変更: 3ステップ

**Step A — limit 到達時に WAL を確定させる**

全ての limit abort パスで、例外を投げる前に現在フェーズの完了済みステップを
WAL に書き込む。これにより「ここまでの成果物」が保全される。

```python
# 変更前
raise LoopLimitExceededError(...)

# 変更後
await self._flush_wal_checkpoint()   # WAL 確定
raise LoopLimitExceededError(...)
```

**Step B — ask_user フックを差し込む**

FP-0003 で提案した budget exceed の ask_user と同じ機構を全 limit に拡張。

```python
async def _handle_limit_exceeded(self, exc, kind: str):
    await self._flush_wal_checkpoint()
    if self._limit_mode == "interactive":
        approved = await self._ask_limit_approval(kind, exc)
        if approved:
            self._extend_limit(kind)
            return  # 続行
    raise exc  # abort（unattended / 拒否）
```

**Step C — 実行モードで挙動を切り替える**

```yaml
# reyn.yaml
safety:
  on_limit:
    mode: interactive   # interactive / unattended / auto-extend
    # interactive:   ask_user で確認（reyn chat デフォルト）
    # unattended:    即 abort（reyn run デフォルト、CI 向け）
    # auto-extend:   自動で N 回延長（信頼済み長時間タスク向け）
    auto_extend_times: 1  # auto-extend の場合の自動延長回数
    ask_timeout_seconds: 60  # interactive の ask タイムアウト（超えたら abort）
```

`reyn run` は `mode: unattended` がデフォルト（既存動作を維持）。
`reyn chat` は `mode: interactive` がデフォルト。

### limit ごとの適用可否

| 機構 | WAL 確定 | ask_user | 理由 |
|---|---|---|---|
| A. max_act_turns | ✅ | ✅ | フェーズ途中でも completed ops は保全可 |
| B. max_phase_visits | ✅ | ✅ | 直前フェーズ完了状態は WAL にある |
| C. router_cap | ✅ | ✅ | ターン内なので ask してから再試行可 |
| D. per_chain_skill_calls | ✅ | ✅ | 起動前なので WAL 確定は即時 |
| E. max_hop_depth | — | ✅ | 委譲拒否、呼び出し元は動いているので ask 可 |
| F. phase_seconds | ✅ | ✅ | 経過時間を延長する形で続行可 |
| G. chain_seconds | ✅ | ✅ | chain timeout を延長して待機継続 |
| H. llm_timeout + retries | 既存 | — | 既に自動再試行あり、ask 不要 |

### 「ここまでの成果物を返す」

abort 時（ユーザーが no と答えた / unattended）でも、
WAL に確定されたフェーズ出力を `RunResult.partial_data` として返す。

```python
class RunResult:
    status: str          # "loop_limit_exceeded" 等
    data: dict | None    # 正常完了時の最終出力
    partial_data: dict | None  # 新規: limit abort 時の途中成果物
    error: str | None
```

ユーザーは `/list` や TUI でこの `partial_data` を確認できる。

---

## FP-0003 / FP-0004 との関係

| FP | 関係 |
|---|---|
| FP-0003（budget 超過時の ask_user）| 本 FP の D（per_chain_skill_calls）の個別実装。本 FP が採択されれば Step B に統合。 |
| FP-0004（safety 設定 UX 改善）| 本 FP の `safety.on_limit.mode` を FP-0004 の `safety:` セクションに追加。相互補完。 |

---

## Dependencies

- `src/reyn/kernel/runtime.py` — `_flush_wal_checkpoint()` + limit abort パスへのフック
- `src/reyn/chat/session.py` — `_ask_limit_approval()` + mode 判定
- `src/reyn/user_intervention.py` / `InterventionBus` — 既存、変更不要
- `src/reyn/schemas/models.py` — `RunResult.partial_data` フィールド追加
- `src/reyn/config.py` — `safety.on_limit` 設定追加
- `src/reyn/chat/services/chain_manager.py` — G（chain timeout）の ask フック

前提 PR: なし。ただし FP-0004（`safety:` セクション）と同時実装が望ましい。

---

## Cost estimate

**合計: LARGE**

| タスク | コスト | 備考 |
|---|---|---|
| Step A: 全 limit abort パスに WAL 確定を挿入 | MEDIUM | 8 箇所、各パスを丁寧に確認 |
| Step B: `_ask_limit_approval()` 共通実装 | SMALL | InterventionBus 呼び出しの共通化 |
| Step B: 各 limit への ask フック差し込み | MEDIUM | limit ごとに挙動が異なるため個別対応 |
| Step C: `on_limit.mode` 設定とデフォルト切り替え | SMALL | config + CLI フラグ |
| `RunResult.partial_data` 追加 + 返却ロジック | SMALL | フィールド追加と abort パスの返却変更 |
| テスト（Tier 1 / Tier 2） | MEDIUM | 各 limit の挙動変化を contract test で担保 |

ボトルネックは **Step A の WAL 確定保証**（現状の abort パスが多様）と
**テスト**（limit 挙動の contract が増える）。

---

## Related

- `src/reyn/kernel/runtime.py` — 現行の limit abort パス
- `src/reyn/events/state_log.py` — WAL 実装
- `src/reyn/user_intervention.py` — InterventionBus
- FP-0003 (`0003-budget-exceed-user-approval.md`) — 本 FP の前身（D 限定版）
- FP-0004 (`0004-safety-config-ux.md`) — `safety:` セクション設計（本 FP と統合対象）
- `docs/concepts/events.md` — P6 イベント設計
- `docs/guide/for-skill-authors/crash-recovery-and-resume.md` — WAL + forward-replay
