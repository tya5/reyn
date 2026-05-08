# S2: concurrent plans — finding doc

| フィールド | 値 |
|---|---|
| Date | 2026-05-08 |
| main HEAD | `4912457` |
| Agent | `b16_s2` |
| Driver | `/tmp/batch16/run_s2.py` |
| Sample size | N=5 |
| Budget | ThreadPoolExecutor(max_workers=2) — 2 HTTP 並列 |
| **verdict 分布** | refuted: 5/5 (100%) |
| **判定** | **R1 リスク的中: plan not invoked (5/5)** + 副次発見として **深刻な concurrent history race (B16-S2-1)** |

---

## 1. サマリー表

| Run | max_concurrent | plans_started | r1_ok | r2_ok | wallclock | verdict |
|---|---|---|---|---|---|---|
| 1 | 0 | 0 | True | True | 7.9s | refuted |
| 2 | 0 | 0 | True | True | 5.4s | refuted |
| 3 | 0 | 0 | True | True | 4.2s | refuted |
| 4 | 0 | 0 | True | True | 5.8s | refuted |
| 5 | 0 | 0 | True | True | 2.9s | refuted |

全 5 run で `plan_started=0`。 `plan` tool は一度も invoke されなかった。

---

## 2. Per-run 詳細

### タイミング (history.jsonl から計測)

| Run | u_gap (P1→P2 到着差) | a_gap (a1→a2 応答差) | total | chain mismatch |
|---|---|---|---|---|
| 1 | 152ms | 963ms | 7.92s | **YES** (2/2) |
| 2 | 139ms | 1241ms | 5.39s | **YES** (2/2) |
| 3 | 128ms | 32ms | 4.20s | ok |
| 4 | 132ms | 2080ms | 5.80s | ok |
| 5 | 120ms | 933ms | 2.89s | ok (swap) |

- **u_gap**: 2 HTTP リクエストが同時発火されても P1・P2 の inbox 記録は 120–152ms 差
  (= Python ThreadPoolExecutor + GIL + asyncio scheduling の現実差)
- **chain mismatch**: `history.jsonl` で user メッセージの `chain_id` と
  対応する agent 返答の `chain_id` が食い違う。 Run 1・2 では 2/2 全チェーンでミスマッチ
- **Run 5**: P1 プロンプトに P2 の答 (CLAUDE.md ルール 3 つ) が返り、
  P2 プロンプトに P1 の答 (src/reyn/ ファイル一覧) が返った (完全逆転)

### reply_first_200 抜粋 (代表)

**Run 1 — r1 (P1 に P1 的答え, 正):**
```
src/reyn/
- `__init__.py`: Reyn の Python パッケージのエントリポイント。…
```

**Run 1 — r2 (P2 に P1 的答え, WRONG):**
```
The files under `src/reyn/` and their roles are:
  * budget: Likely related to budget or cost management…
```
= P2 (CLAUDE.md ルール) を尋ねたのに src/reyn/ ファイル一覧が返った

**Run 5 — r1 (P1 に P2 的答え, WRONG):**
```
Here are the three most important rules from CLAUDE.md:
1. P5 (Workspace is the single source of truth)…
```
= src/reyn/ ファイル一覧を尋ねたのに CLAUDE.md ルールが返った

---

## 3. 何が起きたか (narrative)

### 3-1. R1 リスク的中: plan not invoked (5/5)

P1・P2 プロンプトはいずれも `src/reyn/` のファイル列挙と CLAUDE.md の読み取りという
**情報取得型タスク**。 Router LLM (gemini-2.5-flash-lite) は両プロンプトに対して
`plan` tool を invoke せず、 **直接 text-reply** した (= batch 1-14 で繰り返し観測した
"text-reply attractor" の plan-mode 版)。

`plan_summary` は全 run で `no plan events found` を返した。

P7-clean な観測: どのプロンプトも `file_read` tool すら使わず、 LLM が知識から
回答を生成。 S2 の設計意図 ("2 本の plan が同時起動するか") の検証は、
**そもそも plan が fire する prompts でなければ成立しない**。

### 3-2. 副次発見: concurrent history race (B16-S2-1) — 深刻

ThreadPoolExecutor の 2 スレッドが同一エージェントへ同時 HTTP POST した結果、
`send_to_agent_impl` が **同一 `ChatSession` オブジェクトに対して並列で
`_handle_user_message` を呼び出した**。

`send_to_agent_impl` の実装 (mcp_server.py:154):
```python
baseline = len(session.history)
await asyncio.wait_for(
    session._handle_user_message(message, chain_id=chain_id),
    timeout=timeout,
)
new_replies = _new_agent_history_entries(session, baseline)
```

`_new_agent_history_entries` は `chain_id` でフィルタリングせず、
`baseline` 以降の全 agent エントリを収集する:
```python
for msg in session.history[baseline:]:
    if msg.role == "agent" and msg.text:
        out.append(msg.text)
```

2 リクエストが同時に `baseline = N` を記録すると:

```
req1: baseline=N → _handle_user_message(P1) ─┐
req2: baseline=N → _handle_user_message(P2) ─┤ 同時実行
                                              ↓
history: [N]=user(P1), [N+1]=user(P2), [N+2]=agent(答え), [N+3]=agent(答え)
                                              ↓
req1 → new_replies = history[N:] の agent エントリ全部 → [答え1, 答え2] の JOIN
req2 → new_replies = history[N:] の agent エントリ全部 → [答え1, 答え2] の JOIN
```

加えて、 `_handle_user_message` で history の末尾を参照する部分 (RouterLoop.run()):
```python
if not history or history[-1].get("role") != "user" or history[-1].get("content") != user_text:
    messages.append({"role": "user", "content": user_text})
```
2 コルーチンが同時に `_append_history` でユーザーメッセージを積んだ直後、
両方の RouterLoop が `history[-1]` を参照すると **互いの相手のユーザーメッセージを**
末尾として読む可能性がある。 その結果、 LLM は意図しないプロンプトに答えてしまう。

実際に観察:
- Run 1: chain`66d0f5` (P2 user) の agent エントリが P1 の答え (src/reyn/ 一覧)
- Run 5: P1・P2 が完全逆転

---

## 4. 意味すること

### 4-1. plan 起動条件の再設計が必要 (R1 確定)

S2 設計時の R1 リスク (「Router LLM が plan を invoke しない」) が **5/5 で的中**。
gemini-2.5-flash-lite は情報取得型プロンプトに対して text-reply attractor が支配的。

S2 の本来の目的 (concurrent plans の動作確認) を観測するには:
- より複雑なプロンプト (= multi-step が明示的に必要なタスク)
- または stronger model の使用

を検討する必要がある。

### 4-2. concurrent `_handle_user_message` は unsafe (B16-S2-1 HIGH)

`ChatSession` は asyncio single-event-loop 上で動作するが、
`send_to_agent_impl` が `_handle_user_message` を**直接** await することで、
**複数の FastAPI リクエストハンドラが同一セッションに対して並列 await** できる状態になっている。

asyncio の協調的マルチタスクでは `await` 点でのみタスクスイッチが起きるため、
`_handle_user_message` 内部の IO 待ち (LLM call など) の間に別リクエストが
`_handle_user_message` に入れる。 その結果:

| 問題 | 影響 |
|---|---|
| `history[-1]` の参照競合 | 間違ったユーザーメッセージが LLM コンテキストに入る → 誤答 |
| `_new_agent_history_entries` の chain 非識別 | 別チェーンの返答が混入 → r1 と r2 が同一テキストを受け取る |
| history append の順序不定 | ユーザーメッセージ・エージェント返答の seq が run ごとに異なる順序 |

これは **production 環境で複数クライアントが同一エージェントに並列アクセスした場合**
に常時発生するバグ。 MCP + web server 両方のパスが `send_to_agent_impl` を共有するため、
影響範囲は広い。

### 4-3. async dispatch ≠ concurrent message handling

ADR-0023 Phase 2.1 の async dispatch は **一つの router turn の中での複数 plan 並列起動**
を実装したもの。 これは S2 が意図した「2 メッセージを並列に受け付けて 2 plan を同時起動」
とは異なる。 S2 で観測しようとした concurrency は、 実は Reyn の現アーキテクチャが
明示的に未サポートの動作領域 (= per-session single-turn-at-a-time が前提) だった。

---

## 5. 新 bug

| ID | 重要度 | 内容 | 影響 |
|---|---|---|---|
| **B16-S2-1** | **HIGH** | `send_to_agent_impl` が `_handle_user_message` を lock なしで並列 await 可能 → history race → 誤答・chain 混入 | multi-client (web/MCP) 同時アクセスで常時発生 |
| B16-S2-2 | MED | `_new_agent_history_entries` が chain_id でフィルタリングしない → 並列時に別チェーンの返答が reply_text に混入 | B16-S2-1 の sub-bug。 concurrent 非解消では単独 fix の効果は限定的 |

### B16-S2-1 fix candidates

**Option A (short-term)**: `send_to_agent_impl` に per-session asyncio.Lock を追加し、
同一エージェントへの並列 `_handle_user_message` を serialization する。

```python
# mcp_server.py — per-session lock
_session_locks: dict[str, asyncio.Lock] = {}

async def send_to_agent_impl(registry, *, agent_name, message, timeout):
    lock = _session_locks.setdefault(agent_name, asyncio.Lock())
    async with lock:
        session = await _get_session(registry, agent_name)
        ...
```

デメリット: 並列アクセスが sequential に落ちる (= throughput 低下)。
ただし現 `session.run()` inbox アーキテクチャが serial 前提なので、 これが正しい保守的選択。

**Option B (long-term)**: per-session inbox queue に戻し、 `_handle_user_message` を
inbox 経由に限定。 `send_to_agent_impl` は inbox put + outbox wait で
serial execution を保証。 concurrent messages はキューに積まれ順番に処理される。

---

## 6. Calibration delta

prelude S2 予測:

| 予測 | 実際 |
|---|---|
| verified: 35% | 0% (0/5) |
| inconclusive: 25% | 0% (0/5) |
| refuted: 20% | **100% (5/5)** |
| blocked: 20% | 0% (0/5) |

Brier score (refuted 予測 0.20):

```
B = (0.20 - 1.0)^2 + (0.35 - 0.0)^2 + (0.25 - 0.0)^2 + (0.20 - 0.0)^2
  = 0.64 + 0.1225 + 0.0625 + 0.04
  ≒ 0.875  (= 最悪に近い)
```

calibration 失敗の主因:
1. **R1 リスクを保守的にしか織り込まなかった**: 「plan invoke なし」 を 20% と低く見た。
   batch 1-14 の観測から「情報取得型プロンプトはほぼ always text-reply」 が分かっていたのに
   S2 プロンプト設計の段階で修正しなかった。

2. **concurrent history race の未予測**: `send_to_agent_impl` が lock-free で
   `_handle_user_message` を直接 await する実装は把握していなかった。
   Tier 2 test は single-caller 前提のため race は未カバー。

教訓: 「prompts が plan を trigger するかどうか」 を先に smoke test (= 1 run) で
確認してからシナリオを commit するべきだった (= observe-before-speculate 原則の適用)。

---

## 参照

- `src/reyn/mcp_server.py:128` — `send_to_agent_impl` (lock-free invoke)
- `src/reyn/mcp_server.py:65` — `_new_agent_history_entries` (chain_id 非フィルタ)
- `src/reyn/chat/router_loop.py:392` — `history[-1]` ユーザーメッセージ末尾チェック
- `src/reyn/web/routers/a2a.py:286` — A2A `message/send` handler → `send_to_agent_impl`
- `/tmp/batch16/S2_findings.json` — 生データ
- `/tmp/batch16/S2/run_*/` — per-run snapshot
