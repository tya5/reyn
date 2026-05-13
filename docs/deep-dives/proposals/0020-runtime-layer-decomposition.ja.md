# FP-0020: OSRuntime レイヤ分解 — runtime.py を垂直レイヤに分割する

**Status**: partially-landed — Components A/B/C complete (RunState + LLMCallRecorder + PhaseExecutor extracted, 2026-05-13/14); Component D (RunOrchestrator) remains proposed
**Proposed**: 2026-05-11
**Author**: Research session (eager-shaw-389d9d)

---

## Summary

`src/reyn/kernel/runtime.py` は 1,882 行の単一クラス（`OSRuntime`）に、LLM 呼び出し・
WAL 記録・バジェット強制・フェーズライフサイクル・act/decide ループ・ロールバック・
スキルノードディスパッチ・リジューム早送りが混在している。`session.py`（FP-0019）が
「横並びの無関係な責務」だったのに対し、`runtime.py` の複雑さは**垂直方向**——各層が
その下の層を呼ぶ構造になっている。本提案はその深さを 4 つのファイル——`RunState`・
`LLMCallRecorder`・`PhaseExecutor`・`RunOrchestrator`——に分解し、`runtime.py` 自体を
~400 行に縮小する。5 ファイル合計の行数はわずかに増えるが（~1,620 行 vs 1,882 行）、
各ファイルが独立して読めるようになることが AI コーディングワークフローへの主要な貢献。

---

## Motivation

### AI コーディングにおける分解の意義

AI コーディングエージェントが WAL メモ lookup バグを直すために `runtime.py` を読む場合、
バジェットフック・ロールバック状態・act ターン上限・ポストプロセッサリジューム・MCP 
teardown など、バグと無関係な ~1,900 行をコンテキストに読み込まなければならない。
4 ファイル分解後は `llm_call_recorder.py`（~350 行）を読めば済む。

**合計行数が増えてもよい——1 ファイルあたりの行数を減らすことが目的**。
これが本提案の最優先ゴール。

### 垂直な複雑さ vs 水平な混在

`session.py`（FP-0019）は同じ層に 5 つの無関係な責務が並んでいた。
`runtime.py` は 1 つの責務（スキルを実行する）が 5 つの深さで表現されている：

```
run()                           フェーズを順番にオーケストレート
  └─ _execute_phase()           1 フェーズを完走させる
       └─ _run_act_loop()       act ターンを実行する
            └─ _call_llm_and_record()  LLM を呼んで WAL に記録する
                 └─ _check_budget_pre_llm()  バジェット上限を強制する
```

各層は明確な作業単位を持ち、新しいクラスが始まる自然な継ぎ目がある。

### すでに良い先例: RollbackState

`RollbackState`（L126–221、~95 行）はすでに別クラスとして分離され、
`self._rollback` 経由で使われている。本提案は同じパターンを残りのクラスタに適用する。

---

## Proposed implementation

### Component A — `RunState`（SMALL）— 基盤

**LANDED** (commit `1dac280`): src/reyn/kernel/run_state.py + src/reyn/kernel/rollback_state.py

新規ファイル: `src/reyn/kernel/run_state.py`

`RunState` は純粋な dataclass（イベントなし・I/O なし）で、1 回の `run()` 実行に
対応するすべてのミュータブル状態を保持する。全レイヤーが同一の `RunState` 参照を受け取り、
名前付きメソッドを通じてインプレースで変更する。

**フィールド**（10 個）:

```python
@dataclass
class RunState:
    # ナビゲーション（RunOrchestrator が所有）
    visit_counts: dict[str, int]
    history: list[str]
    prev_phase: str | None
    rollback: RollbackState          # すでに分離済みのクラス

    # per-phase ライフサイクル（begin_phase() がリセット）
    phase_started_at: float | None
    llm_call_idx_in_phase: int

    # run レベルのアキュムレータ
    token_usage: TokenUsage
    total_cost_usd: float

    # Safety extensions（FP-0005）
    # キーの名前空間:
    #   "max_phase_visits"         — ループ上限（run スコープ）
    #   "phase_seconds"            — 壁時計予算（run スコープ）
    #   "max_act_turns:{phase}"    — act ターン上限（フェーズスコープ）
    safety_extensions: dict[str, float]

    # 信頼入力（PR33 — run() 先頭で1回セット、LLM は改竄不可）
    skill_input: dict | None
```

**メソッド**（~12 個）: `begin_phase()`、`next_llm_invocation_id()`、
`elapsed_phase_seconds()`、`reset_phase_clock()`、`add_usage()`、
`grant_extension()`、`effective_visit_cap()`、`effective_phase_budget()`、
`effective_act_turn_cap()`、`record_transition()`、`restore_from_resume()`。

`restore_from_resume(plan, default_phase)` は R-D2 の pre-decrement パターンを封入する:
リジューム時に `visit_counts[current_phase]` を 1 減らしてから `begin_phase()` を呼ぶことで、
リジューム後の最初の LLM 呼び出しが元の実行と同じ `op_invocation_id` に対応し、
WAL メモ lookup が正しく機能する。

目標: `src/reyn/kernel/run_state.py` — ~100 行

### Component B — `LLMCallRecorder`（SMALL）— Layer 3

**LANDED** (commit `5628993`): src/reyn/kernel/llm_call_recorder.py

新規ファイル: `src/reyn/kernel/llm_call_recorder.py`

担当: バジェット事前チェックから WAL 記録までの LLM 呼び出し 1 回。

```python
class LLMCallRecorder:
    def __init__(self, *, resolver, state_log, run_id, skill_registry,
                 budget_tracker, caller, chain_id, skill_name,
                 prompt_cache_enabled, events, skill): ...

    async def call(
        self,
        phase: str,
        frame: ContextFrame,
        prior_attempts: list[dict] | None,
        rollback_context: dict | None,
        state: RunState,
    ) -> dict:
        """バジェット確認 → メモ lookup → call_llm → WAL 記録 → usage 積算"""
```

抽出メソッド: `_call_llm_and_record`、`_wal_step_completed_for_llm`、
`_extract_memoized_llm_result`、`_credit_budget_from_memo`、`_budget_agent_name`、
`_check_budget_pre_llm`、`_record_budget_post_llm`。

フェーズバジェットチェック（`_check_phase_budget`）は **PhaseExecutor（Layer 2）へ引き上げる**。
これにより `LLMCallRecorder` は `phase_started_at` を知らなくてよくなる。
この移動が本提案で唯一の振る舞いの変更——チェックが `_call_llm_and_record` 内ではなく
PhaseExecutor レベルで行われるが、観測可能な動作は同一。

目標: `src/reyn/kernel/llm_call_recorder.py` — ~350 行

### Component C — `PhaseExecutor`（SMALL）— Layer 2

**LANDED** (commit `7e51216`): src/reyn/kernel/phase_executor.py + src/reyn/kernel/runtime_types.py（= 循環インポート回避のリーフモジュール）

新規ファイル: `src/reyn/kernel/phase_executor.py`

担当: act/decide ループとリトライによる 1 フェーズの完走。

```python
class PhaseExecutor:
    def __init__(self, *, llm_caller: LLMCallRecorder, control_ir_executor,
                 events, skill, safety, intervention_bus): ...

    async def execute(
        self,
        phase: str,
        artifact: dict,
        candidates: list[CandidateOutput],
        output_language: str | None,
        max_phase_retries: int,
        artifact_path: str | None,
        rollback_context: dict | None,
        state: RunState,
    ) -> tuple[NormalizationResult, LLMOutput, int]:
        """Act ループ → Decide ループ → (result, output, retry_count) を返す"""
```

抽出メソッド: `_run_act_loop`、`_run_decide_with_retry`、`_execute_phase`、
`_check_phase_budget`（`LLMCallRecorder` から移動）。

`_validate_phase_output` もここに移動（現在は `OSRuntime` のメソッドだが
`_run_decide_with_retry` からしか呼ばれない）。

目標: `src/reyn/kernel/phase_executor.py` — ~270 行

### Component D — `RunOrchestrator`（MEDIUM）— Layer 1

新規ファイル: `src/reyn/kernel/run_orchestrator.py`

担当: フェーズ順序・遷移・ロールバックディスパッチ・スキルノードディスパッチ・
リジュームセットアップ・SkillRegistry ライフサイクル・例外ハンドリング。

```python
class RunOrchestrator:
    def __init__(self, *, phase_executor: PhaseExecutor, skill, workspace,
                 events, skill_registry, preprocessor, state: RunState,
                 safety, intervention_bus, resume_plan, run_id,
                 parent_run_id): ...

    async def run(
        self,
        initial_input: dict,
        output_language: str | None,
        max_phase_retries: int,
    ) -> RunResult: ...
```

抽出メソッド: `_enter_phase`、`_handle_limit_checkpoint`、`_handle_rollback`、
`_finish_workflow`、`_fallback_final_output`、`_run_skill_node`、`_apply_skill_node`、
`run()` の本体（現在 415 行、リジューム早送りを含む）。

目標: `src/reyn/kernel/run_orchestrator.py` — ~500 行

### 抽出後の OSRuntime

`OSRuntime` は配線レイヤーになる：

```python
class OSRuntime:
    def __init__(self, skill, model, ...):
        state = RunState()
        llm_caller = LLMCallRecorder(...)
        phase_exec = PhaseExecutor(llm_caller=llm_caller, ...)
        self._orchestrator = RunOrchestrator(phase_executor=phase_exec, state=state, ...)
        # 後方互換のため公開: workspace, events, control_ir_executor

    async def run(self, initial_input, ...) -> RunResult:
        return await self._orchestrator.run(initial_input, ...)

    # 残留: build_frame(), _build_candidates(), _effective_model()
```

`runtime.py` に残るもの: 例外・型定義 / `OSRuntime.__init__` 配線 / `build_frame()` + 
`_build_candidates()` + `_effective_model()` / `run()` 委譲。

目標: `src/reyn/kernel/runtime.py` — ~400 行

---

## 行数サマリ

```
分離前（提案時のベースライン）
  runtime.py            1,882 行

Component A/B/C 着地後（現在 — Component D は proposed）
  runtime.py            1,386 行   （配線 + build_frame + 型定義; Component D 後の予測 ~1,490 行）
  run_state.py            166 行   （新規 — Component A、実測）
  rollback_state.py       111 行   （新規 — Component A、設計時の想定外の派生 entry）
  llm_call_recorder.py    415 行   （新規 — Component B、実測）
  phase_executor.py       500 行   （新規 — Component C、実測）
  runtime_types.py        105 行   （新規 — Component C、循環インポート回避のリーフモジュール）
  ──────────────────────────────
  A/B/C 着地小計        2,683 行（6 ファイル）

Component D 着地後（予測）
  runtime.py             ~400 行   （予測）
  run_orchestrator.py    ~500 行   （予測、新規 — Component D）
  ──────────────────────────────
  合計（予測）          ~1,697 行
```

A/B/C 着地後の現状: **runtime.py は 496 行削減**（1,882 → 1,386 行）。
Component D 着地で さらに ~986 行削減し ~400 行に到達する見込み。

**ゴールは 1 ファイルあたりの行数最小化であり、合計行数の最小化ではない。**
最大ファイルサイズが半減するなら合計 +10% は許容コスト。

---

## 優先順位

**A → B → C → D**

`RunState`（A）は基盤——他のコンポーネントはすべてこれを受け取る。
`LLMCallRecorder`（B）は B・C・D の中で依存が最も少なく、価値が最も高い
（WAL + バジェットを 1 つのテスト可能ユニットに）。
`PhaseExecutor`（C）は B に依存。
`RunOrchestrator`（D）は B + C に依存し、最大のピース——最後に着手する。

各ウェーブは振る舞いの変更なしに独立した PR としてリリースできる。

---

## Dependencies

- **Component A**: 外部依存なし。即時着手可能。
- **Component B**: A（RunState）が必要。
- **Component C**: A + B が必要。
- **Component D**: A + B + C が必要。
- **FP-0019**（session.py リファクタ）: 独立。並行して進められる。
- **FP-0012**（非同期実行）: LANDED（commit `c9e79d6`）。Component B + C が安定した
  ユニットとして分離されることで、着地済みの非同期タスクインフラが LLM 呼び出しと
  フェーズ境界を明確なターゲットとして参照できるようになる。

---

## Cost estimate

| コンポーネント | コスト | 備考 |
|---|---|---|
| A: `RunState` | SMALL | 純粋データ + メソッド、振る舞い変更なし |
| B: `LLMCallRecorder` | SMALL | メソッド抽出; `_check_phase_budget` を 1 層上に移動 |
| C: `PhaseExecutor` | SMALL | メソッド抽出 + `_validate_phase_output` 移転 |
| D: `RunOrchestrator` | MEDIUM | 最大の抽出; リジューム + ライフサイクルの複雑さ |
| テスト（新クラスの Tier 1） | SMALL | LLMCallRecorder の独立テストが特に価値高い |
| **合計** | **MEDIUM** | |

Component A 単独は SMALL で、有効化基盤として先行リリース可能。

---

## Related

- `src/reyn/kernel/runtime.py` — 抽出元（1,882 行）
- `src/reyn/kernel/run_state.py` — 新規（Component A）
- `src/reyn/kernel/llm_call_recorder.py` — 新規（Component B）
- `src/reyn/kernel/phase_executor.py` — 新規（Component C）
- `src/reyn/kernel/run_orchestrator.py` — 新規（Component D）
- FP-0019 (`0019-chat-session-refactor.md`) — session.py の並行 God-file 削減
- ADR-0029（Permission モデル）— `PhaseExecutor` が `ControlIRExecutor` にパーミッション宣言を渡す
- FP-0017 (`0017-sandboxed-execution.md`) — Component D 着地（commit `ddf2d05`）: `exec.py`
  に `DeprecationWarning` 追加済み。`PhaseExecutor` 抽出時は deprecated `exec` op ではなく
  `sandboxed_exec` を使用すること
