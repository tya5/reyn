# S4: Operator Commands — Batch 16 Findings

| Field | Value |
|---|---|
| Date | 2026-05-08 |
| main HEAD | `f4952af` |
| Scenario | S4 — `/plan list` / `/plan discard` / `/plan resume --from` がスラッシュコマンドとして機能するか |
| Agent | `b16_s4` |
| Sample size | N=5 (Phase A probe + Phase B 5 runs) |
| **Verdict breakdown** | **refuted: 5 / verified: 0 / inconclusive: 0 / blocked: 0** |

---

## 1. Summary Table

| 項目 | 予測 | 実測 |
|---|---|---|
| verified | 25% (1.25/5) | 0% (0/5) |
| inconclusive | 20% (1/5) | 0% (0/5) |
| refuted | 45% (2.25/5) | 100% (5/5) |
| blocked | 10% (0.5/5) | 0% (0/5) |
| slash_works_over_a2a | — | **False** (Phase A probe で確定) |
| plan invoked (N=5) | 55% | 0% |
| total elapsed | — | 16.1s (avg 3.2s/run) |
| est. S4 cost | ~$0.005 | ~$0.005 (Phase A 1 call + 5 trigger calls) |

S4 の測定は 2 層構造:
1. **Phase A**: slash-over-A2A が機能するか (1 probe)
2. **Phase B**: plan trigger prompt でプランが起動するか (N=5)

いずれも否定結果。 refuted 5/5 は (a) slash が A2A 経由で応答不可 かつ (b) plan tool が real LLM で invoke されないという 2 つの独立したブロッカーの合成。

---

## 2. Phase A: Slash-over-A2A Probe

### 観測

`/plan list` を A2A JSON-RPC (`message/send`) で `b16_s4` に送信した結果:

```
ok=True elapsed=0.1s
reply: ''   ← 空文字列
```

返答は空文字列。 LLM の自然言語返答でも、 slash handler のフォーマット出力でも
なく、 **完全な無応答**。 slash_works_over_a2a = **False**。

### 根本原因: 二重バリア

ソースコード分析で 2 層の阻害を確認した。

**バリア 1 — `is_attached=False` でのメッセージ破棄**

`ChatSession._put_outbox` (session.py:1168):

```python
if not self.is_attached and msg.kind in {"status", "trace"}:
    return   # ← slash reply が ここで drop される
```

`is_attached` は TUI が `session.is_attached = True` をセットしたときのみ True
(registry.py:785)。 A2A の `send_to_agent_impl` は `is_attached` を触らないため、
A2A セッションでは常に False。 slash の `reply()` が `kind="status"` で発行する
OutboxMessage は全てこの分岐でドロップされる。

**バリア 2 — 返答収集経路が `role="agent"` 限定**

`_new_agent_history_entries` (mcp_server.py:65):

```python
for msg in session.history[baseline:]:
    if msg.role == "agent" and msg.text:   # ← slash reply は history に入らない
        out.append(msg.text)
```

slash コマンドは `_put_outbox` → outbox キューに積むのみで、`_append_history` は
呼ばない。 たとえバリア 1 を通過しても、 A2A の返答収集パスは history を読むため
slash reply は永遠に届かない。

### コード実行フロー対照

| 経路 | slash dispatch | is_attached | outbox drop | history append | A2A 可視 |
|---|---|---|---|---|---|
| TUI (reyn chat) | ✓ | True | なし | なし (outbox 描画) | N/A |
| A2A (send_to_agent_impl) | ✓ | **False** | **破棄** | なし | **不可** |

slash コマンドのディスパッチ自体は A2A 経由でも発生する (`_handle_user_message` の
`text.startswith("/")` 分岐は共通)。 ただし返答がどのチャネルにも届かない。

---

## 3. Phase B: Per-Run Details

| Run | Verdict | plan_invoked | WAL plan_events | Elapsed | reply_len | Note |
|---|---|---|---|---|---|---|
| 1 | refuted | False | 0 | 6.2s | 774 | 直接 file list 返答。 cold start |
| 2 | refuted | False | 0 | 3.5s | 774 | 全 run で同じ 774 字の list を返答 |
| 3 | refuted | False | 0 | 3.2s | 774 | plan tool invoke なし |
| 4 | refuted | False | 0 | 2.0s | 774 | 同上 |
| 5 | refuted | False | 0 | 1.3s | 774 | warm cache でさらに高速化 |

トリガープロンプト:
```
src/reyn/ 以下の Python ファイルを読んで、ファイル名と簡単な説明を一覧にして
```

dogfood_trace plan-summary の全 run 出力:
```
no plan events found (no plan-mode runs recorded)
```

reply_len が全 run 一致 (774 字) している点から、 LLM がプロンプトを
「single-tool retrieval タスク」と判定して training-data から直接 list を生成している
可能性が高い。 実際に `file_read` tool も invoke されていない。

---

## 4. What Happened

### ブロッカー 1: slash_works_over_a2a = False

S4 のシナリオは A2A ドライバからのスラッシュコマンド送信を前提としていたが、
実際には slash reply が A2A レイヤに到達しない。 これはアーキテクチャ上の分離であり
バグではなく **設計上のスコープギャップ**:

- TUI / CLI は `is_attached=True` + outbox 購読で slash reply を受け取る
- A2A / MCP は `is_attached=False` + history 収集パスで LLM reply のみを受け取る
- 「operator コマンド」は TUI/CLI を持つオペレータ向けの概念であり、
  A2A エージェント間プロトコルで使うことは設計上想定されていない

### ブロッカー 2: plan tool が real LLM で invoke されない

S1/S2/S5 と同様、 `gemini-2.5-flash-lite` は `plan` tool を 0/5 で invoke しない。
run 1 の 6.2s elapsed はプロンプトへの直接応答として自然な速度。 warm cache
での run 5 (1.3s) は回答内容が run 1 と一致しており、 tool 呼び出しなし。

2 つのブロッカーは独立している:
- plan tool が invoke されても、 slash コマンドは A2A から操作できない
- slash が A2A で届いても、 plan が起動しなければ list/discard/resume の対象がない

### 代替観測パス

slash コマンドが届かない場合の等価観測は `dogfood_trace.py --mode plan-summary` で
可能。 ただし N=5 全てで plan_events = 0 のため discard/resume の観測機会なし。

---

## 5. New Bugs / Findings

### [ARCH] S4-A1: A2A で slash コマンドが応答不可 (設計ギャップ)

| 項目 | 詳細 |
|---|---|
| ID | S4-A1 |
| 重要度 | ARCH (= 設計上の境界、 immediate fix 不要だが文書化必要) |
| 現象 | A2A JSON-RPC で `/plan list` を送っても空返答。 slash reply が `kind="status"` として `_put_outbox` に到達するが `is_attached=False` でドロップ |
| 根拠コード | `session.py:1178` (is_attached guard) + `mcp_server.py:67` (role="agent" filter) |
| 設計上の意図 | slash コマンドはオペレータが TUI/CLI から発行するもの。 A2A は LLM-to-LLM / driver-to-agent プロトコルで operator UI を持たない |
| 影響 | A2A ドライバから operator コマンドをテストすることが不可能。 dogfood では TUI attach が必要 |
| 推奨対応 | (a) 文書化のみ (現状維持) か (b) A2A 向けに `kind="status"` を `kind="agent"` にプロモートする option を設ける。 優先度 LOW |

### [HIGH] S4-B2: plan tool が real LLM 環境で invoke されない (S1/S2/S5 と同根)

S1-B16-S1-2 と同根。 `gemini-2.5-flash-lite` が plan tool invoke 閾値を超えない。
batch 16 の全シナリオ (S1/S2/S4/S5) で共通の阻害要因。 S4 固有の新バグではなく
既知バグの再確認。

---

## 6. Calibration Delta

| 予測 | 実測 | Brier component |
|---|---|---|
| verified 25% | 0/5 (0%) | (0.25-0)² = 0.0625 |
| inconclusive 20% | 0/5 (0%) | (0.20-0)² = 0.04 |
| refuted 45% | 5/5 (100%) | (0.45-1.0)² = 0.3025 |
| blocked 10% | 0/5 (0%) | (0.10-0)² = 0.01 |
| **Brier score** | — | **0.415** (= 4 class 平均: 0.104) |

S1 Brier 0.70 よりは改善 (refuted 予測を 30% → 45% に修正していた) が、
依然として refuted の過小評価が残る。

**キャリブレーション更新**:

S4 の特異点は「slash コマンドが A2A で機能しない」という arch 制約が事前に
分からなかった点。 これにより:
1. "slash_works=True" を前提とした "verified" 予測が根拠を失った
2. "refuted" の主因が **plan_trigger 失敗** + **slash_arch_gap** の 2 独立事象

次回 slash コマンドシナリオを再設計する場合の修正方針:
- TUI attach ドライバ (= Textual pilot または subprocess で `reyn chat` を制御)
  を使うか、 Web UI の WebSocket channel 経由で slash を送る
- または plan が起動した後に file inspection + `dogfood_trace` だけで operator
  コマンドの **効果** を検証する (= slash の可視性ではなく state 変化を測る)
- plan tool invoke 問題は S4 以前に解決が必要 (= 先行 blocker)
