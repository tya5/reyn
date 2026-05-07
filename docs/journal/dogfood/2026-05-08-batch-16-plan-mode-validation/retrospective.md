# Batch 16 (plan-mode validation — first real LLM dogfood) — Retrospective

> **「plan-mode 呼び出しギャップ発見」 milestone** —
> "plan-mode 検証完了" ではなく "plan tool が real LLM で呼ばれないことを data で確定した"
> milestone として位置づける。 30+ commit の plan-mode infra は Tier 2 で全て PASS している。
> しかし real LLM が plan tool を 0/25 で invoke しなかった事実が、 batch 16 の最大発見。
> これは失敗ではなく **data-driven な課題特定**。 次 batch はこの gap を埋める設計に集中できる。

---

## 想定と現実のずれ

### 開始時の想定

batch 1-14 での G1/G23 attractor 観測を踏まえ、 plan tool に対しても保守的な
refuted prior (S1: 30%、 S2: 20%、 S3: 15%、 S4: 10%、 S5: 15%) を設定した。
それでも verified 率を S1: 40%、 S2: 35%、 S3: 45%、 S4: 50%、 S5: 55% と
見積もっており、 overall での Brier target は 0.30 以下。

なぜそう想定したか:

- batch 1-14 の G1 (= router が specialist skill を invoke しない) attractor は
  30-40% 前後の base rate として経験則的に把握していた
- plan tool は "skill invocation" ではなく "tool invocation" であり、
  attractor 強度は異なると仮定した
- 30+ commit が Tier 2 で validate 済みであること (= 実装は正しい) が、
  real LLM がそれを使うかどうかの prior を楽観方向に引いた

### 実際の進行

| 想定 | 現実 |
|---|---|
| refuted rate (各 S): 10-30% 程度 | **refuted: 5/5 (100%) × 全 5 シナリオ** |
| plan invoked: 14-27 / 25 runs | **plan invoked: 0 / 25 runs** |
| Brier 平均: ~0.30 | **Brier 平均: 0.96** (= batch 14 の 0.18 から大幅悪化) |
| cost: 設計 ADR 規模 相応 | **~$0.04** (= plan steps 未発火で LLM call が 1/run に留まったため格安) |
| 3 シナリオ以上で verified 4/5+ | **0 シナリオが verified** |

plan tool が real LLM 環境で一度も invoke されなかったことは、 batch 設計の
中心的前提を崩す結果。 Tier 2 tests は pass しているが、 real LLM との接続が
成立していないか、 real LLM に plan tool を使わせる条件が整っていない。

### なぜ予測が外れたか

**主因 1: R1 attractor の强度を過小評価した**

batch 1-14 での G1 attractor (skill 非 invoke) は 30-40% の base rate として
記録されていたが、 plan tool は "rare" multi-step tool であり、 skill router の
attractor と質的に異なる。 LLM が "いつも直接回答する" パターンは、
「既知の simple tool call パス (file_read 等) が存在する」 場合よりも
「plan という 迂回路 を使う必要がある」 と判断するために、 はるかに強い
contextual signal が必要と考えられる。

**主因 2: prompt engineering と tool invocation の関係を軽視した**

各シナリオのプロンプトは「情報取得型タスク」 が中心だった。 LLM は training-data
knowledge から直接回答できる質問に対して plan を invoke する動機を持たない。
「2 読み込み + 1 合成」 という構成が human には multi-step に見えても、
LLM には「1 回の知識応答」として見えている。

**主因 3: tool description の言語 mismatch (en vs ja)**

plan tool description は English で記述されているが、 プロンプトは全て日本語。
weak LLM (gemini-2.5-flash-lite) では tool description の en/ja mismatch が
invoke 閾値に影響する可能性を事前に考慮しなかった。

---

## ターニングポイント 3 つ

### TP1: smoke S1 が 1 run で R1 を確認した時点での判断

batch 16 の最初の smoke run (S1 single run、 default agent) で plan が invoke
されなかったことは、 A3 dispatch 前に判明していた。 この時点での選択肢は:

- **Option A**: 1-run smoke で R1 確定と判断 → LLM context dump を優先调査 → 25 runs を実施しない
- **Option B**: 実施した選択 — N=25 を dispatch して rate data を収集する

N=25 を実施した根拠は「1 run では R1 確定に不十分」 という数字への慎重さだが、
振り返ると theoretical attractor knowledge が十分だった:

- 「情報取得型プロンプト + weak LLM = text-reply attractor」 は batch 1-14 で
  documented design として確立していた
- smoke で refuted が出た場合、 追加 N が attractor の depth を変えることはない
- 結果: 25 runs 全てが同一パターンを再現し、 rate data は「0/25」 という
  1-run smoke と同等の情報を提供した

**教訓**: smoke が理論的 attractor と整合する場合、 N=25 を確認のために消費するより
root-cause investigation (= LLM context dump、 tool description 確認) に先に
ピボットする方が効率的。 「1 sample は too few」 ルールは、 signal が理論と
反する可能性がある時に適用する。 signal が理論と整合する時は 1 sample で commit できる。

### TP2: B16-S2-1 が plan なしの concurrent drive 自体で surface した

S2 は "concurrent plans の観測" が目的だったが、 plan が一度も invoke されなかった。
にもかかわらず、 ThreadPoolExecutor × 2 で同一エージェントに並列 HTTP POST した
副産物として、 **`send_to_agent_impl` の concurrent history race** (B16-S2-1) が
表面化した。

これは unplanned scope: dogfood driver 自体が production infra の integration test
として機能した。 S2 sonnet がシナリオ設計の意図外で production HIGH bug を発見した。

Tier 2 tests は single-caller を前提としており、 この race は未カバー。
A2A 経由のマルチクライアント同時アクセスという production 現実のパスを、
dogfood driver の構造が初めて踏んだ。

**教訓**: dogfood driver の concurrency pattern 自体が integration test。
"driver が何を test しているか" は scenario goal と独立に分析する価値がある。
A2A concurrent dispatch は S2 goal を達成しなかったが、 infra layer の
critical bug を発見した — これは dogfood discipline の正当な価値。

### TP3: B16-S4-1 (slash-over-A2A gap) がアーキテクチャ境界を明確化した

S4 sonnet の Phase A probe (1 shot) で `/plan list` の A2A 経由送信が空返答になることを
確認した。 `session.py` のコードを読んで 2 つのバリアを特定:

1. `is_attached=False` での `kind="status"` メッセージ破棄
2. `_new_agent_history_entries` の `role="agent"` フィルタ

これは バグではなく設計上の意図した分離。 operator コマンドは TUI/CLI が持つ
「オペレータ」向け UI であり、 LLM-to-LLM チャネルである A2A にそれを通す
設計意図は最初からない。

しかし dogfood harness の設計 (= A2A driver) がこの境界を事前に考慮しておらず、
S4 シナリオ全体が「A2A では達成できない目標」を設定していた。

**教訓**: driver mechanism の選択は scenario 設計より先に行う。
A2A は LLM-to-LLM / マルチエージェント観測に適するが、
operator コマンド (= slash) のテストには TUI attach またはサブプロセス制御が必要。
次回の operator コマンドシナリオは driver から設計する。

---

## 教訓

### 教訓 1: plan tool の R1 attractor base rate は ≥ 80% と更新する

batch 1-14 の G1 attractor (skill 非 invoke) base rate は 30-40% で記録されてきた。
しかし plan tool は multi-step 迂回路という性格上、 同等以上の attractor strength を
持つ。 batch 16 で 0/25 (= 100% refuted) を観測した。

今後の plan-mode dogfood prelude の prior 設定:
- plan tool refuted: **≥ 80%** (= batch 16 の実測を反映)
- 情報取得型プロンプトでは **≥ 95%** refuted と見積もる
- 「plan を invoke するよう設計された prompt」 で初めて refuted 50% 以下が期待できる

この prior の更新は giveup-tracker への G24 登録を含む。

### 教訓 2: dogfood は scenario ではなく harness 自体の integration test

B16-S2-1 (concurrent history race) と B16-S4-1 (slash-over-A2A gap) はいずれも
**scenario の目標とは無関係に** 発見された。 前者は driver の concurrency pattern が
production infra を踏んだ副産物、 後者は scenario 設計前提の誤りを確認するプローブの副産物。

dogfood の価値は "LLM が設計どおりに動くか" の観測だけでなく、
"dogfood driver が通るコードパスが Tier 2 tests が通るコードパスと異なるか" にある。

今後の dogfood planning に追加するチェック:
- 「このドライバは何を integration test しているか?」 を scenario goal と別に明示する
- driver concurrency / multi-agent / timeout abort などの driver-specific patterns を
  "副次的に観測する infra component" として事前に列挙する

### 教訓 3: 「数字に踊らされない」 原則 — 1 sample が理論と整合する時は commit できる

memory `feedback_envelope_layer_fix.md` に記録された "数字に踊らされる trap" の
バリアント: 「1 sample は too few → N=25 で確認」 という慎重さが、
理論的 attractor knowledge がある場合には過剰消費になる。

判断 framework:
1. smoke result が理論的 attractor 知識と **整合する** → 1 sample で pivot commit できる
2. smoke result が理論と **反する** → N 追加で確率を測定する
3. smoke result が **ニュートラル** (= attractor 方向不明) → N 追加が有効

batch 16 の smoke は理論と整合していた (「情報取得 + weak LLM = text-reply」)。
1 sample で root-cause investigation へのピボットが正当化されていた。

### 教訓 4: ADR-0023 §3.4 deferral は継続が正しい

batch 16 で plan tool が 0/25 で invoke されなかった事実は、
sub-loop tool-op memoization (ADR-0023 §3.4) の実装 priority を下げることを
さらに強く支持する。 memoize すべき sub-loop LLM call が発生する前に、
plan tool 自体の invocation 問題を解決する必要がある。

継続 defer の根拠が batch 16 で data-backed になった。

### 教訓 5: A2A driver が適する場面と適さない場面を明示化する

| 観測目標 | 適合 driver | A2A 可否 |
|---|---|---|
| LLM-to-LLM マルチエージェント通信 | A2A | ✓ |
| concurrent message handling の infra 挙動 | A2A (副次的に観測可能) | ✓ |
| plan tool invoke / plan runtime E2E | A2A | ✓ (ただし plan が fire する前提) |
| operator slash コマンド (/plan list 等) | subprocess + TUI attach | ✗ |
| SIGKILL crash recovery + auto-resume | dedicated subprocess | ✗ |
| Tier 2 単体ロジック確認 | pytest | N/A |

次 batch の harness 設計前にこの表を参照し、 scenario × driver の適合性を先に確認する。

---

## 修正分類サマリ

batch 16 で landing した変更の全体像:

| 区分 | 内容 | ステータス |
|---|---|---|
| **Driver fix** | B16-S1-1: `clean_state()` に `history.jsonl` + `outbox.jsonl` + `memory/` の wipe を追加 | `/tmp/batch16/driver.py` に反映済み。 次 batch で repo 化 |
| **OS code 変更** | なし | batch 16 は仕様変更ゼロ |
| **Tracker 登録** | B16-S2-1 (HIGH), B16-S4-1 (ARCH) → 次 batch fix 候補 | 登録予定 |
| **Doc 更新** | A2A driver 適合表、 prelude への R1 base rate 注記 | このレトロスペクティブで記録 |

= **batch 16 は仕様変更ゼロ batch + 2 production bug surface batch + R1 base rate 更新 batch**

infra 30+ commit (ADR-0022/0023/0024/0025) は全て Tier 2 PASS 維持。
real LLM が invoke しなかった事実は implementation の問題ではなく、
real LLM を plan tool に誘導する mechanism の欠如。

---

## Brier スコア内訳

### Per-scenario 予測 vs 実測

| Scenario | 予測: verified | 予測: refuted | 実測 verdict | Brier score |
|---|---|---|---|---|
| S1 (multi-source synthesis) | 40% | 30% | refuted 5/5 (100%) | 0.70 |
| S2 (concurrent plans) | 35% | 20% | refuted 5/5 (100%) | 0.88 |
| S3 (crash + resume) | 45% | 15% | refuted 5/5 (100%) | 1.08 |
| S4 (operator commands) | 50% | 10% → 45%* | refuted 5/5 (100%) | 0.42 |
| S5 (large output spill) | 55% | 15% | refuted 5/5 (100%) | 1.08 |
| **平均** | — | — | — | **0.83** |

*S4 は S1 観測後に refuted prior を 45% に上方修正した後の Brier で計算。 原 prelude prior では 0.64。

### バッチ間 Brier 比較

| Batch | Brier | 主因 |
|---|---|---|
| 8 | 0.96 | 累積 fix verify の over-confidence |
| 9 | 0.55 | wrong layer trap 学習 |
| 10 | 0.30 | verify-first framework 導入 |
| 11 | 0.65 | N=1 lucky case を base rate に使った overestimate |
| 12 | 0.40 | batch 11 教訓反映 |
| 13 | 0.20 | documented design 整合性 audit、 best |
| **14** | **0.18** | 8 batch 中 best |
| **16** | **0.83** | **R1 plan attractor の base rate を大幅過小評価** |

batch 16 は batch 8 に匹敵する Brier 悪化。 ただし batch 8 は "fix が本当に
landing しているか" の calibration failure、 batch 16 は "real LLM が new tool を
invoke するか" の calibration failure — 異なる種類の miscalibration。

batch 16 の miscalibration は future batch の prior 設定に直接 feed する。
plan-mode 観測 batch の refuted prior は今後 ≥ 80% に設定する。

---

## 次 batch (batch 17) への申し送り

### Immediate (= batch 17 prep 必須)

**1. R1 plan attractor 調査: なぜ gemini-2.5-flash-lite は plan を invoke しないか**

3 つの角度から調査する:

- **LLM context dump の検証**: `REYN_LLM_TRACE_DUMP` を使って、 plan tool definition が
  LLM に送信される system prompt + tool catalog に含まれているかを確認。
  "tool not registered" vs "tool registered but not chosen" を分離する。
- **description rewrite trial**: plan tool description を日本語を含む wording に変更し、
  または "このツールを使うべきケース" の具体例を追加して invocation rate を測定。
- **tool-forcing mechanism**: テスト専用の "must-use-plan" prefix を prompt に注入し、
  plan runtime の動作観測を可能にする。 production では使わないが、
  infra の E2E 動作確認には有効。

**2. B16-S2-1 fix layer 1 (per-session asyncio.Lock) を landing させる**

production 環境でマルチクライアントが同一エージェントに並列アクセスした場合に常時発生する
HIGH bug。 MCP + web 両パスが `send_to_agent_impl` を経由するため影響範囲は広い。

Fix candidate (short-term):
```python
# src/reyn/mcp_server.py
_session_locks: dict[str, asyncio.Lock] = {}

async def send_to_agent_impl(registry, *, agent_name, message, timeout):
    lock = _session_locks.setdefault(agent_name, asyncio.Lock())
    async with lock:
        ...
```

**3. G24 plan-mode-router-attractor を giveup-tracker に追加**

giveup-tracker に以下を記録:
- G24: plan tool が weak LLM で invoke されない (R1 base rate ≥ 80%)
- 調査前提: tool description が LLM context に届いているかを verify-first
- 解決方針候補: description rewrite / prompt prefix / model upgrade

### 次 batch 候補 (batch 17)

| 優先 | 候補 | 内容 |
|---|---|---|
| HIGH | R1 root cause 調査 | LLM context dump で tool visibility 確認 |
| HIGH | B16-S2-1 fix + verify | per-session lock → dogfood E2E で concurrent access が clean になるか確認 |
| MED | tool-forcing prompt 試行 | plan invocation を強制して plan runtime の E2E を観測 |
| MED | S3 dedicated subprocess harness | SIGKILL crash recovery を A2A 制約外で観測 |
| LOW | S5 prompt redesign | 明示的 multi-step prompt で verbosity ≥ 32 KB を誘発 |

### Tracker (longer-term)

| 項目 | 判断 |
|---|---|
| slash-over-A2A architecture (B16-S4-1) | サポートするか non-support を文書化するかを決定。 LOW priority |
| ADR-0023 §3.4 sub-loop tool-op memo | defer 継続。 R1 が解消されるまで実装 priority なし |
| B16-S3-1 history bleed race | driver.py に post-clean wait を追加。 LOW priority |

---

## Outstanding 質問

### Q1: R1 plan attractor は ADR 化すべき structural 問題か

"gemini-2.5-flash-lite が plan tool を invoke しない" は:

- **known LLM-bias artifact** として扱い、 prompt engineering / model upgrade で解決を試みる → ADR 不要
- **structural understanding として ADR 化** し、 "Reyn は weak LLM に plan tool を invoke させるための explicit mechanism を必要とする" と定義する → ADR-0026 候補

現段階では前者が有力だが、 tool-forcing 試行の結果次第で判断する。

### Q2: concurrent A2A access (B16-S2-1) はサポートユースケースか

per-session asyncio.Lock で serialization すれば technically safe になるが:

- "1 エージェントには 1 turn ずつ" を production 設計として文書化して、
  concurrent access を explicitly 非サポートにするか
- per-session queue に戻して concurrent message を inbox に積む long-term fix を採用するか

この判断は production deployment model (= 1 agent への concurrent access が
expected かどうか) に依存する。 MCP server 経由なら concurrent が現実的。

### Q3: plan-mode dogfood のコスト vs 観測効率 のトレードオフ

R1 attractor が blocking している限り:
- N=25 は $0.04 で済む (= plan steps が firing しないため)
- しかし観測情報はほぼゼロ

R1 を解消するための tool-forcing や model upgrade のコストをどの程度まで許容するか。
gemini-2.5-flash-lite 以外 (例: gemini-2.5-flash や claude-haiku-3.5) では
plan invocation rate が変わるかを spike で確認する価値があるか。

---

## 一言で

> **R1 attractor に block されて plan tool 0/25 invoke — 「30+ commit の plan-mode
> infra は Tier 2 PASS だが real LLM 経路は未通過」 という課題が data で確定、
> B16-S2-1 concurrent history race (HIGH) + B16-S4-1 slash-over-A2A (ARCH) を
> 副次発見した batch**

— Brier 0.83 (= batch 8 並みの calibration regression)、 主因は R1 prior の過小評価
— plan-mode infra の問題ではなく "LLM を plan invoke に誘導する mechanism" の欠如
— batch 17 は R1 root-cause 調査 + B16-S2-1 fix が最優先
— "smoke 1 sample で理論整合 → 即ピボット" の教訓を次 batch の dispatch 設計に組み込む
