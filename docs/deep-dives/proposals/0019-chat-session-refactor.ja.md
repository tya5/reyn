# FP-0019: ChatSession 責務分離 — session.py からのサービス抽出

**Status**: partially-landed — Wave 1+2 part 1+3 complete (CompactionController + SkillRunner + InterventionHandler + AutoResumeHandler extracted, 2026-05-13/14); Wave 2 part 2 (A2AHandler) proposed (次 wave)
**Proposed**: 2026-05-11
**Author**: Research session (eager-shaw-389d9d)

---

## Summary

`src/reyn/chat/session.py` は 3,836 行に膨れ上がり、`ChatSession` 内に 5 つの独立した
責務が混在している: スキル実行管理・A2A エージェントプロトコル・インターベンション
ルーティング・コンパクション・オートリジューム。合計 2,122 行の 6 つのサービスクラスは
すでに `src/reyn/chat/services/` に抽出済みだが、残存ロジックは依然として密結合であり、
安全に修正できる状態ではない。本提案は 3 段階のウェーブで抽出を完了させ、`session.py`
を ~600 行の薄いディスパッチャ——抽出済みのサービスに全委譲するだけのもの——に縮小する。

---

## Motivation

### 現状

```
src/reyn/chat/
├── session.py              3,836 行   ← このFPのターゲット
└── services/
    ├── budget_gateway.py     347 行   ┐
    ├── chain_manager.py      412 行   │
    ├── intervention_registry.py  298 行 │ 抽出済み
    ├── memory_service.py     389 行   │ （合計 2,122 行）
    ├── router_host_adapter.py 334 行  │
    └── snapshot_journal.py   342 行   ┘
```

### session.py に残存している責務

`ChatSession` 内にまだ 5 つの凝集したクラスタが埋め込まれている:

| クラスタ | 主要メソッド | 責務 |
|---|---|---|
| SkillRunner | `running_skills`、`_run_stdlib_skill`、`_dispatch_routing_decision_for_user` | スキルタスクのライフサイクル（起動 / 追跡 / キャンセル） |
| A2AHandler | `_send_to_agent`、`_send_agent_response`、`_handle_agent_request`、`_handle_agent_response`、`_resolve_pending_chain` | エージェント間プロトコル（送信 / 受信 / チェーン） |
| InterventionHandler | `_maybe_answer_oldest_intervention`、`_dispatch_intervention`、`_announce_intervention`、`_wait_for_intervention_answer` | ユーザー向け ask_user フロールーティング |
| CompactionController | `_maybe_compact`、`_run_compaction`、`_compaction_task` | コンテキストコンパクションのスケジューリングと実行 |
| AutoResumeHandler | `_auto_resume_active_skills` | クラッシュ回復——セッション開始時に WAL からスキルを再起動 |

### 今これが重要な理由

- **FP-0012（非同期実行）** は LANDED（commit `c9e79d6`）。Wave 1 の `SkillRunner`
  抽出は、着地済みの非同期 OS と `session.py` を整合させる——非同期タスクインフラは
  OS 側に存在するが、`session.py` はまだスキルタスク dict をモノリシックに所有している。
  `SkillRunner` を抽出することでその境界が明確になる。
- **FP-0011（narrator 廃止）** は、現在 `_dispatch_routing_decision_for_user` の中に
  埋め込まれたルーティングパスへの変更を必要とする。先に `SkillRunner` を抽出する
  ことでこの変更のリスクが下がる。
- **テスタビリティ**: 現在の `session.py` は 5 つの責務がすべて `self` を共有して
  いるため、意味のあるユニットテストが書けない。抽出後は各サービスを狭い API に対して
  独立してテストできる。
- **オンボーディング**: A2A プロトコルのバグを直したいコントリビュータは、現在
  3,836 行を読まなければならない。抽出後は A2A の表面が ~350 行の自己完結ファイルになる。

### 設計制約

- 抽出されたサービスは依存関係をインジェクション（event_log、agent_config など）
  で受け取る。循環インポートなし、グローバルシングルトンなし。
- `ChatSession.run()` メッセージループは最上位ディスパッチャとして `session.py` に残る。
- 各サービスは抽出済みの 6 サービスと同じパターンに従う:
  `__init__` が型付きの依存関係を受け取り、スコープ外の `ChatSession` の `self` を
  直接参照しない。
- P6: すべての状態変更はイベント発行を維持する。振る舞いの変更なし、構造的な移動のみ。
- テストはテスティングポリシーに従い Tier 1（サービスコントラクト）と Tier 2
  （OS 不変条件）で記述する。モックは使用しない。

---

## Proposed implementation

### Wave 1 — SMALL × 2（最低リスク、FP-0011 を解除し着地済み FP-0012 と整合する）

**CompactionController** (`services/compaction_controller.py`)

**LANDED** (commit `6620505`): src/reyn/chat/services/compaction_controller.py

抽出対象: `_maybe_compact`、`_run_compaction`、`_compaction_task`

```python
class CompactionController:
    def __init__(self, *, llm_client, event_log, config, snapshot_journal): ...
    async def maybe_compact(self, messages: list[Message]) -> list[Message]: ...
    async def cancel(self) -> None: ...
```

コンパクションはスキルルーティングや A2A との結合がない純粋なバックグラウンド関心事。
抽出により、`ChatSession` が直接所有している唯一のバックグラウンド `asyncio.Task` が
なくなる。

**SkillRunner** (`services/skill_runner.py`)

**LANDED** (commit `9ae66fa`): src/reyn/chat/services/skill_runner.py

抽出対象: `running_skills` dict、`_run_stdlib_skill`、`_dispatch_routing_decision_for_user`

```python
class SkillRunner:
    def __init__(self, *, agent_config, event_log, workspace_root, permission_checker): ...
    async def dispatch(self, decision: RoutingDecision, *, chain_id: str) -> None: ...
    async def cancel(self, skill_name: str) -> None: ...
    async def cancel_all(self) -> None: ...
    def running_names(self) -> list[str]: ...
```

`SkillRunner` は着地済み FP-0012 の非同期 OS が期待する表面: `dispatch()` が同期実行・
非同期実行両方のエントリポイントとなる明確な境界を提供する。`cancel_all()` はセッション
シャットダウン時に呼ばれる。

対象ファイル:
- `src/reyn/chat/services/compaction_controller.py` — 新規ファイル
- `src/reyn/chat/services/skill_runner.py` — 新規ファイル
- `src/reyn/chat/session.py` — インジェクションを配線、抽出済みメソッドを削除

### Wave 2 — MEDIUM × 2（A2A とインターベンションは結合が強い。Wave 1 安定後に実施）

**A2AHandler** (`services/a2a_handler.py`)

抽出対象: `_send_to_agent`、`_send_agent_response`、`_handle_agent_request`、
`_handle_agent_response`、`_resolve_pending_chain`

```python
class A2AHandler:
    def __init__(self, *, agent_registry, event_log, chain_manager): ...
    async def send(self, target_agent: str, payload: A2APayload, *, chain_id: str) -> None: ...
    async def receive_request(self, payload: A2APayload) -> None: ...
    async def receive_response(self, payload: A2APayload) -> None: ...
```

A2A プロトコルは完全に自己完結している（送信 / 受信 / チェーン解決）。
`ChatSession` への唯一の結合は `chain_manager` 依存（抽出済み）。

**注意**: FP-0013（unified-inbox-outbox-transport、ACCEPTED）は A2A 送受信が乗る
トランスポート層を再構成する。`A2AHandler` 抽出は FP-0013 の実装と同じ PR か直後に
着地すべき——FP-0013 着地前にこの抽出を行うと、A2A インターフェースの再修正が必要になる。

**InterventionHandler** (`services/intervention_handler.py`)

**LANDED** (commit `11d96dc`): src/reyn/chat/services/intervention_handler.py

抽出対象: `_maybe_answer_oldest_intervention`、`_dispatch_intervention`、
`_announce_intervention`、`_wait_for_intervention_answer`

```python
class InterventionHandler:
    def __init__(self, *, intervention_registry, event_log, skill_runner): ...
    async def maybe_answer(self, text: str) -> bool: ...
    async def dispatch(self, iv: Intervention) -> InterventionAnswer: ...
```

`InterventionHandler` は抽出済みの `InterventionRegistry` と Wave 1 の `SkillRunner`
に依存する。したがってこのウェーブは Wave 1 の完了前に開始できない。

**A2AHandler** 抽出は **proposed** (Wave 2 part 2) — FP-0013 が全 LANDED 済みのため、
次 wave での着地を予定。

対象ファイル:
- `src/reyn/chat/services/a2a_handler.py` — 新規ファイル（proposed、次 wave）
- `src/reyn/chat/services/intervention_handler.py` — LANDED commit `11d96dc`
- `src/reyn/chat/session.py` — インジェクションを配線、抽出済みメソッドを削除

### Wave 3 — SMALL（クリーンアップウェーブ、FP-0011 着地に合わせて延期）

**AutoResumeHandler** (`services/auto_resume_handler.py`)

**LANDED** (commit `ba7f7c3`): src/reyn/chat/services/auto_resume_handler.py

抽出対象: `_auto_resume_active_skills`

```python
class AutoResumeHandler:
    def __init__(self, *, skill_runner, event_log, wal_reader): ...
    async def resume_active(self) -> int: ...  # 再起動したスキルの件数を返す
```

`AutoResumeHandler` は Wave 1 の `SkillRunner` に依存する。その抽出は FP-0011
（narrator 廃止）と結合していた——FP-0011 が先行 land、Wave 3 は同 wave で commit
`ba7f7c3` に着地済み。

対象ファイル:
- `src/reyn/chat/services/auto_resume_handler.py` — LANDED commit `ba7f7c3`
- `src/reyn/chat/session.py` — インジェクションを配線、抽出済みメソッドを削除

### Wave 3 完了後の目標状態

**現在地**: Wave 1+2 part 1+3 完了（2026-05-13/14）。Wave 2 part 2（A2AHandler）のみ次 wave で proposed。

```
src/reyn/chat/
├── session.py              ~600 行   （メッセージループ + 依存関係の配線のみ）
└── services/               合計 ~3,800 行
    ├── budget_gateway.py
    ├── chain_manager.py
    ├── intervention_registry.py
    ├── memory_service.py
    ├── router_host_adapter.py
    ├── snapshot_journal.py
    ├── compaction_controller.py   ← Wave 1（LANDED commit 6620505）
    ├── skill_runner.py            ← Wave 1（LANDED commit 9ae66fa）
    ├── intervention_handler.py    ← Wave 2 part 1（LANDED commit 11d96dc）
    ├── auto_resume_handler.py     ← Wave 3（LANDED commit ba7f7c3）
    └── a2a_handler.py             ← Wave 2 part 2（proposed、次 wave）
```

`session.py` は薄い配線レイヤーになる: `__init__` で全サービスを初期化し、
`run()` が受信メッセージを適切なサービスメソッドにルートする。
`session.py` 自体にはビジネスロジックが一切残らない。

---

## 優先順位

**Wave 1 → Wave 2 → Wave 3**

Wave 1（CompactionController + SkillRunner）は最小実行可能な抽出:
着地済み FP-0012 の非同期 OS と session.py を整合させ、変更の爆発半径を縮小する。
Wave 2 は Wave 1 の `SkillRunner` が安定していることを前提とし、FP-0013 と連携する。
Wave 3 は FP-0011 と結合しており、待機できる。

---

## Dependencies

- **Wave 1**: 外部 FP 依存なし。即時着手可能。
- **Wave 2**: Wave 1 完了が必要（InterventionHandler が SkillRunner に依存）。
  `A2AHandler` 抽出は FP-0013（unified-inbox-outbox-transport、ACCEPTED）と連携すべき。
- **Wave 3**: Wave 1 完了 + FP-0011 着地推奨（narrator パス削除）。
- **FP-0012**（非同期スキル実行）: LANDED（commit `c9e79d6`）。Wave 1 は
  `session.py` 側を着地済みの非同期 OS プリミティブと整合させる。

---

## Cost estimate

| ウェーブ | コンポーネント | コスト |
|---|---|---|
| 1 | CompactionController 抽出 | SMALL |
| 1 | SkillRunner 抽出 | SMALL |
| 2 | A2AHandler 抽出 | MEDIUM |
| 2 | InterventionHandler 抽出 | MEDIUM |
| 3 | AutoResumeHandler 抽出 | SMALL |
| 全体 | テスト（Tier 1: サービスコントラクト） | SMALL |
| **合計** | **3 ウェーブ** | **MEDIUM** |

Wave 1 単独は SMALL で独立してリリースできる。~600 行への完全抽出は全体で MEDIUM。

---

## Reyn 原則との整合

本 FP は純粋に構造的なものであり、原則違反の導入も解消もない。
動機はメンテナビリティと着地済み FP-0012（P3 / P6 クリーンな非同期実行モデル）との
整合にある。

---

## Related

- `src/reyn/chat/session.py` — 抽出元（3,836 行）
- `src/reyn/chat/services/` — 抽出済みサービス（6 ファイル、2,122 行）
- FP-0011 (`0011-remove-narrator.md`) — Wave 3 との結合（AutoResumeHandler の narrator パス）
- FP-0012 (`0012-async-skill-execution.md`) — LANDED commit `c9e79d6`; Wave 1 が session.py を非同期 OS と整合
- FP-0013 (`0013-unified-inbox-outbox-transport.md`) — ACCEPTED; Wave 2 の A2AHandler 抽出と連携が必要
