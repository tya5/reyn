# Batch 16 (plan-mode validation — first real LLM dogfood) — Findings

> **5/5 refuted** across all scenarios (N=25 total runs)。
> `gemini-2.5-flash-lite` は plan tool を **0/25 で invoke せず**、
> 全 prompt に対して text-reply attractor が支配的だった。
> plan 起動ゼロにもかかわらず、 dogfood は **5 件の新 bug / gap** を surfacing — うち
> 2 件 (B16-S2-1、 B16-S4-1) は production 環境に影響する HIGH / ARCH 問題。

---

## 1. Headline / TL;DR

batch 16 は plan-mode 実装 (ADR-0022/0023/0024/0025 + Phase 2.1 async dispatch、
30+ commit、 Tier 2 のみ検証済み) を **初めて real LLM + real I/O** に晒す batch だった。

結果:

- **G1 問い「Router LLM は plan tool を自律的に invoke するか」**: **5/5 シナリオ × N=5 = 0/25 で invoke されず**
- **R1 リスク (prelude §8) が 100% rate で的中**: `gemini-2.5-flash-lite` via LiteLLM proxy は
  multi-step synthesis / crash-resume / 32KB spill 向けプロンプトを含む全 25 run で
  text-reply attractor から抜け出せなかった
- **plan 起動ゼロにもかかわらず 5 件の新 bug が浮上**: B16-S2-1 (concurrent history race)
  + B16-S4-1 (slash-over-A2A arch gap) の 2 件は batch 14 までのシナリオでは
  観測不可能だったシステム設計上の欠陥
- **Brier 0.96**: batch 13-14 の 0.20 水準から大幅悪化。 plan-mode dogfood では
  tool-forcing なしなら refuted prior ≥ 80% が適切と判明

---

## 2. Per-scenario summary table

| シナリオ | N | 分布 (verified/refuted/inconclusive/blocked) | ヘッドライン発見 | 新 bug |
|---|---|---|---|---|
| **S1** multi-source synthesis | 5 | 0/5/0/0 (refuted 100%) | plan invoke ゼロ + history.jsonl bleed が N=5 独立性を破壊 | B16-S1-1 (HIGH driver)、 B16-S1-2 (MED investigation) |
| **S2** concurrent plans | 5 | 0/5/0/0 (refuted 100%) | plan invoke ゼロ + concurrent A2A access が history race を引き起こし誤答 | **B16-S2-1 (HIGH production)** |
| **S3** crash + resume | 5 | 0/5/0/0 (refuted 100%) | plan invoke ゼロ + A2A 制約下では kill-9 / process-restart を模擬できない構造的限界 | B16-S3-1 (MED driver) |
| **S4** operator commands | 5 | 0/5/0/0 (refuted 100%) | plan invoke ゼロ + slash コマンドが A2A レイヤに到達しない 2 重バリア確認 | **B16-S4-1 (ARCH)** |
| **S5** large output spill | 5 | 0/5/0/0 (refuted 100%) | plan invoke ゼロ + 直接回答が 4,215 bytes (threshold 32,768 の 13%) で spill 条件未到達 | (新バグなし、 S5 prompt 設計の revisit 要) |

---

## 3. R1 attractor — 支配的 signal の詳細

### 観測事実

- **25/25 run で plan tool 未 invoke** (= `plan_created` WAL event ゼロ)
- `dogfood_trace.py --mode plan-summary` の全 run 出力: `no plan events found`
- LLM は `file_read` tool すら呼ばず、 training-data knowledge のみで回答
- S4 は run 2-5 で reply_len が全く同一 (774 bytes) — **warm cache pattern** を示す
  (= LLM が同一 prompt への返答をキャッシュして即答)

### G1/G23 attractor family との連続性

batch 1-14 で `skill_router` LLM が skill tool を invoke しない attractor (G1/G23) を
繰り返し観測してきた。 batch 16 ではその **plan-mode 版** が顕現した:

| batch | attractor 対象 | invoke rate | fix |
|---|---|---|---|
| 1-6 | skill_router → skill tool | < 20% | G1/G23 各種 fix |
| 7-14 | skill_router (安定化後) | 80-100% | R1 fix (B13-NEW-1 等) |
| **16** | **router → plan tool** | **0%** | **未 fix (G24 候補)** |

### 根本原因の仮説 (= 観測 infra 未整備のため仮説段階)

1. **Prompt language × tool description language mismatch**: ユーザー prompt が日本語、
   plan tool description が英語。 gemini-2.5-flash-lite の cross-lingual tool-use
   能力が弱い可能性
2. **LLM の "direct completion" bias**: 1 shot で完結できると判断したタスクに対して
   tool を invoke しない。 プロンプトが「何を生成すべきか」は明確でも「plan を使うべきか」は
   non-obvious
3. **tool description の nudge 強度不足**: plan tool description が
   「いつ plan を使うべきか」の判断基準を十分に表現できていない

> 観測 infra (`REYN_LLM_TRACE_DUMP` による system prompt + tool catalog 全記録) を
> 整備して payload を直接確認してから根本原因を確定させる (= observe-before-speculate 原則)。

---

## 4. Production bugs

### B16-S2-1 [HIGH] — concurrent A2A history race

**症状**: 2 つの A2A クライアントが同一エージェントに同時 HTTP POST すると、
互いの返答が cross-talk する (= P1 への返答に P2 の answer が混入、 逆も然り)。

**証拠** (S2 finding doc §2-3):

- Run 1: chain `66d0f5` (P2 user) の agent entry が P1 の答え (`src/reyn/` 一覧)
- Run 5: P1・P2 の返答が完全逆転 (P1 には CLAUDE.md ルール、 P2 には src/reyn/ 一覧)

**根本原因** (2 層):

```
send_to_agent_impl (mcp_server.py:154):
  baseline = len(session.history)          ← 2 コルーチンが同時に同値を取得
  await session._handle_user_message(...)  ← lock なしで並列 await 可能
  new_replies = _new_agent_history_entries(session, baseline)

_new_agent_history_entries (mcp_server.py:65):
  for msg in session.history[baseline:]:
      if msg.role == "agent" and msg.text:  ← chain_id フィルタなし
          out.append(msg.text)
```

asyncio の協調的マルチタスクでは `await` 点でのみタスクスイッチが起きる。
`_handle_user_message` 内の LLM call (= IO 待ち) 中に別リクエストが同一セッションに
入れるため、 history の append 順序と `history[-1]` 参照が race する。

**影響**: MCP + web server 両パスが `send_to_agent_impl` を共有するため、
**production multi-client 環境では常時発生し得る**。

**fix sketch** (2 層):

Layer 1 (短期 — 正確性優先): `send_to_agent_impl` に per-session `asyncio.Lock` を追加し、
同一エージェントへの `_handle_user_message` 並列実行を serialization する。
throughput は下がるが、 現 `session.run()` inbox アーキテクチャが serial 前提なので
保守的かつ正しい選択:

```python
_session_locks: dict[str, asyncio.Lock] = {}

async def send_to_agent_impl(registry, *, agent_name, message, timeout):
    lock = _session_locks.setdefault(agent_name, asyncio.Lock())
    async with lock:
        session = await _get_session(registry, agent_name)
        ...
```

Layer 2 (長期 — 並行性維持): `ChatMessage.meta` に `chain_id` を tag し、
`_new_agent_history_entries` が `chain_id` でフィルタリングする。
concurrent message handling を保ちつつ cross-talk を排除。

**推奨**: Layer 1 を先に着地させ正確性を確保、 Layer 2 を follow-up PR で実装する。

---

### B16-S4-1 [ARCH] — slash コマンドが A2A 経由で応答不可

**症状**: A2A JSON-RPC で `/plan list` を送信しても空返答 (`reply=''`)。
slash handler の dispatch 自体は実行されるが、 reply がどのチャネルにも届かない。

**根本原因** (2 重バリア):

```
バリア 1 — ChatSession._put_outbox (session.py:1168):
  if not self.is_attached and msg.kind in {"status", "trace"}:
      return   # slash reply (kind="status") をドロップ

is_attached は TUI が registry.py:785 でセットする。
A2A の send_to_agent_impl は is_attached を触らない → 常に False。

バリア 2 — _new_agent_history_entries (mcp_server.py:65):
  if msg.role == "agent" and msg.text:
      ...  # slash reply は history に入らない (outbox only)
```

**設計の境界**:

これは strict なバグではなく **アーキテクチャ上のスコープ境界**:
- slash コマンドは **operator (TUI/CLI を持つ人間)** 向けの概念
- A2A は **LLM-to-LLM / driver-to-agent** プロトコルで operator UI を持たない設計
- TUI path (`is_attached=True`) は outbox 購読で slash reply を受け取る — 機能はしている

しかし **dogfood ドライバが A2A を経由する限り、 slash コマンドの効果を観測できない**。
plan-mode の operator コマンド (`/plan list` / `discard` / `resume`) は
TUI attach が必要な機能として文書化する必要がある。

**fix candidates**:

| 選択肢 | 内容 | 優先度 |
|---|---|---|
| (a) 文書化のみ | slash = TUI/CLI 専用 concept として concept doc + ADR に記述 | 推奨 (現実的) |
| (b) `/observe/agents/<name>/state` GET endpoint | plan / skill state を REST で公開。 dogfood が slash なしで状態を観測可能 | MED (dogfood 継続に有効) |
| (c) A2A reply promotion | `kind="status"` を A2A response では `kind="agent"` にプロモート | LOW (設計変更大) |

---

## 5. Driver / methodology バグ

### B16-S1-1 [HIGH driver] — `clean_state` が `history.jsonl` を wipe しない

`clean_state(agent_name)` が `state/` + `events/` を削除するが、
`history.jsonl` (= agent_dir root に存在) は保持されていた。
結果として N=5 が independent fresh run でなく **単一 growing session** として動作し、
Run 2/4 は「既に回答済み」英語返答 (= history 参照) となった。

**修正** (S2 以降の driver.py に適用済み):
`clean_state` に `history.jsonl` + `outbox.jsonl` + `memory/` の wipe を追加する。
scope は driver.py のみ — OS コード変更不要。

---

### B16-S3-1 [MED driver] — `clean_state` と server-side delayed write の race

HTTP timeout abandon (8s) 後もサーバーは LLM 処理を継続し、
`clean_state()` が history.jsonl を wipe した後に別 run の server-side agent entry が
追記される場合がある。 S3 の Run 4 で observe: follow-up reply が先行 run 内容を参照。

**workaround**:
- abandon 後の `clean_state()` 前に ≥ 5s wait を挟む
- または S3 専用 subprocess harness (別ポート) で server-side 干渉を排除する

---

## 6. S5 verbosity calibration

ADR-0024 の 32 KB threshold は batch 16 では実質テスト不能だった。
理由は 2 層:

1. **plan tool が invoke されなかった** (= spill ロジックに到達しない)
2. **LLM が直接回答しても 4,215 bytes** (= threshold の 13%) — `src/reyn/` に 186 Python
   ファイルが存在するが、 LLM は tool なしでは immediate-level の 10 ファイルのみ列挙した

S5 のプロンプト設計を batch 17 向けに改訂する必要がある:

改訂案 A (plan step 明示型):
「次の 3 ステップで実行してください: ステップ 1: src/reyn/ 以下の全 .py ファイルを列挙 (サブディレクトリ含む)、 ステップ 2: 各ファイルのクラス名・主要メソッド・役割を 2-3 文で説明、 ステップ 3: 全ファイルの説明を 1 つのレポートに統合」

改訂案 B (controlled spill trigger):
既知の大きなファイル (例: events.jsonl / trace log) を plan step input として使い、
LLM verbosity に依存しない spill 条件を作る。

ADR-0024 実装は `80e4977` で landing 済み、 Tier 2 test は pass している。
batch 16 での未観測は「code defect」でなく「coverage gap」。

---

## 7. Calibration delta

### Brier score breakdown

| シナリオ | verified 予測 | refuted 予測 | blocked 予測 | Brier (4-class sum) |
|---|---|---|---|---|
| S1 | 40% | 30% | 10% | **0.70** |
| S2 | 35% | 20% | 20% | **0.865** |
| S3 | 45% | 15% | 15% | **1.01** |
| S4 | 50% | 10% | 15% | **1.145** |
| S5 | 55% | 15% | 10% | **1.075** |
| **平均** | — | — | — | **0.959** |

(各 Brier は prelude 元予測と実測 [refuted 100%] の 4-class sum)

**avg Brier 0.96**: batch 8 の initial 水準 (0.96) に逆戻り。 batch 13-14 で達成した
0.20 水準 (= calibration discipline の成果) が plan-mode 新設計で reset された。

### メタ教訓

> **plan-mode dogfood の refuted prior は、 tool-forcing が存在しない限り ≥ 80% を
> default とすべき**。 「複雑なプロンプトなら plan を使うはず」という期待は
> `gemini-2.5-flash-lite` に対して成立しない。

キャリブレーション失敗の構造的原因:

1. **R1 リスクを各シナリオで独立に「保守的」と見積もった**: S1 30%、 S2 20%、 S3 15%、
   S4 10-45%、 S5 15%。 実際は全シナリオで 100%。 cross-scenario 相関を prior に入れるべきだった
2. **「plan tool が新規追加だから attractor の degree は未知」を楽観的に読んだ**:
   「未知 = 最悪ケース 100% の可能性あり」と対称に読むべきだった
3. **observe-before-speculate の不適用**: S2/S4 commit 前に 1 smoke run で plan invoke を
   確認していれば、 batch 設計を大幅に変えられた

---

## 8. Action items

### HIGH (immediate)

| ID | 内容 | 参照コード |
|---|---|---|
| A16-1 | **B16-S2-1 Layer 1 fix**: `send_to_agent_impl` に per-session `asyncio.Lock` を追加 | `src/reyn/mcp_server.py:154` |
| A16-2 | **G24 候補登録**: R1 plan-mode attractor を giveup-tracker に追記 (= tool-forcing なし環境での plan invoke 0% を正式に記録) | `docs/journal/dogfood/giveup-tracker.md` |
| A16-3 | **slash-over-A2A を architectural decision として文書化**: concept doc (en + ja) に「slash コマンドは TUI/CLI 専用」を記述 | `docs/en/concepts/plan-mode.md` |

### MED (next batch)

| ID | 内容 | 参照コード |
|---|---|---|
| A16-4 | **B16-S2-1 Layer 2**: `ChatMessage.meta` に `chain_id` tag + `_new_agent_history_entries` フィルタ追加 | `src/reyn/mcp_server.py:65` |
| A16-5 | **S3 専用 subprocess harness**: 別ポートで dedicated web server を起動し、 SIGKILL crash → resume を安全に観測できる環境を設計 | — |
| A16-6 | **S5 プロンプト改訂**: plan step 明示型 prompt または controlled spill trigger に変更し再設計 | `findings/S5-large-output-spill.md` §6 |
| A16-7 | **`/observe/agents/<name>/state` GET endpoint 検討**: slash なしで plan state を A2A / REST から観測できる軽量 endpoint | `src/reyn/web/routers/` |

### LOW (tracker)

| ID | 内容 |
|---|---|
| A16-8 | plan tool description の language / wording を見直し (= 日本語プロンプト × 英語 tool description の mismatch 仮説の検証) |
| A16-9 | `reyn.yaml` への tool-forcing mechanism の検討 (= predictable dogfood のための optional config) |
| A16-10 | `REYN_LLM_TRACE_DUMP` による system prompt + tool catalog payload 観測 infra を S1 再実施前に整備し、 plan tool が LLM に届いているかを直接確認 |

---

## 9. 参照

| ドキュメント | パス |
|---|---|
| prelude | `docs/journal/dogfood/2026-05-08-batch-16-plan-mode-validation/prelude.md` |
| S1 finding | `findings/S1-multi-source-synthesis.md` |
| S2 finding | `findings/S2-concurrent-plans.md` |
| S3 finding | `findings/S3-crash-resume.md` |
| S4 finding | `findings/S4-operator-commands.md` |
| S5 finding | `findings/S5-large-output-spill.md` |
| batch 14 findings (比較基準) | `../2026-05-06-batch-14-stability-extension/findings.md` |
| giveup-tracker | `../giveup-tracker.md` |
| ADR-0022 crash fail-safe | `docs/en/decisions/0022-plan-mode-crash-fail-safe.md` |
| ADR-0023 forward replay | `docs/en/decisions/0023-plan-mode-forward-replay.md` |
| ADR-0024 step result spill | `docs/en/decisions/0024-plan-step-result-spill.md` |
| ADR-0025 sub-loop LLM memo | `docs/en/decisions/0025-plan-step-llm-memoization.md` |
| plan-mode concept (en) | `docs/en/concepts/plan-mode.md` |
| dogfood discipline | `docs/en/contributing/dogfood-discipline.md` |

---

## 一言で

> **5/5 refuted (N=25 全 run) = R1 attractor が plan-mode でも 100% 支配的、
> tool-forcing なし環境での plan invoke は成立しない —
> しかし B16-S2-1 (concurrent history race) と B16-S4-1 (slash-over-A2A arch gap) の
> 2 件の深刻問題が dogfood でしか見えなかった production risk として浮上、
> Brier 0.96 で calibration は batch 8 水準に逆戻り**

— plan tool が invoke されなくても、 dogfood は production risk を surfacing する
— R1 attractor は plan-mode でも G1/G23 family と同根。 tool-forcing が前提条件
— batch 17 に向けて: A16-1 (lock fix)、 A16-2 (G24 tracker)、 A16-3 (arch doc) を先行着地させ、
  S1 再設計 + observe infra 整備を経て plan invoke 観測を再試みる
