# S3: Crash + Resume — Batch 16 Findings

| フィールド | 値 |
|---|---|
| Date | 2026-05-08 |
| main HEAD | `f4952af` |
| Agent | `b16_s3` |
| Driver | `/tmp/batch16/run_s3.py` |
| Sample size | N=5 |
| Method | HTTP timeout abandon (8s) + 15s server-side wait + follow-up prompt |
| **verdict 分布** | **refuted: 5/5 (100%)** |
| **判定** | **R1 リスク的中: plan not invoked (5/5)** + A2A制約下での crash 観測の根本的限界を文書化 |

---

## 1. サマリー表

| Run | abandon_ok | abandon_elapsed | plan_triggered | fu_ok | fu_elapsed | verdict |
|---|---|---|---|---|---|---|
| 1 | True (8s以内に完了) | 3.4s | False | True | 1.6s | refuted |
| 2 | False (TimeoutError) | 8.0s | False | True | 5.4s | refuted |
| 3 | False (TimeoutError) | 8.0s | False | True | 4.9s | refuted |
| 4 | True (8s以内に完了) | 1.3s | False | True | 10.0s | refuted |
| 5 | True (8s以内に完了) | 5.4s | False | True | 1.5s | refuted |

全 5 run で `plans_dir` にファイルなし、`plan_snapshot` 生成なし。
`plan` tool は一度も invoke されなかった。

`plan_summary` コマンド出力: `no plan events found (no plan-mode runs recorded)`

---

## 2. Per-run 詳細

### Run 1: abandon 前に完了 (3.4s)

- long prompt 送信後 3.4s で HTTP 応答返却。 8s timeout 内に LLM が直接 text-reply を完了。
- client-side から見れば "abandon" は発生していない (ok=True)。
- follow-up: 「さっきの作業はどうなった？」→ 「どの作業のことですか？」(コンテキスト喪失)。
- 観察: clean_state() が history.jsonl を wipe したため、 follow-up の LLM は先行する turn を参照できない (B16-S1-1 と同様の history bleed inverse — clean_state が正常に機能したケース)。

### Run 2: TimeoutError (8.0s) — server-side 継続

- 8s で client-side TimeoutError。 server-side では LLM 処理が継続。
- 15s wait 後: plans/ ディレクトリなし。
- history.jsonl の timeline 分析: seq 5 (Run 2 の long prompt) が `23:30:42` に記録。
  Client が abandon した後もメッセージが server-side キューで処理された。
- follow-up (seq 6) は `23:31:05` — 15s wait 終了後に正常送信。
  agent は「src/reyn/ 以下のファイルを確認し agent.py と config.py を見つけた」と返答。
  plan mode は 0 step。 直接 text-reply attractor。

### Run 3: TimeoutError (8.0s) — server-side 継続

- Run 2 と同パターン。 Follow-up で「agent.py と config.py しかアクセスできなかった」と謝罪。
- history.jsonl がクリーンアップされていたため Run 2 の内容を知らないが、
  LLM は training-data knowledge から Reyn の構造を推定し、
  存在しないファイルへの言及 (constants.py, llm.py 等) を含む応答を返した。

### Run 4: abandon 前に完了 (1.3s)

- 1.3s で完了 (= 最速応答)。 plan tool 呼び出しゼロ、直接 text-reply。
- follow-up は 10.0s かかった — 先の Run 3 の long prompt processing が
  server-side queue に残留し、 follow-up が queued state で待機した可能性。
- reply: 「src/reyn/ を再検討した」とのコンテキスト付き回答。
  これは clean_state() 後の history.jsonl への Run 3 server-side 処理の書き込みが
  follow-up の turn に混入した可能性を示す (= history bleed race condition)。

### Run 5: abandon 前に完了 (5.4s)

- 5.4s で完了。 follow-up 1.5s で「どの作業ですか？」とコンテキスト喪失返答。
- clean_state() が正常に機能し history がクリアされた後の典型パターン。

---

## 3. 何が起きたか

### 主要観察: plan never triggered (5/5)

S1/S2 と同一パターン。 router LLM は全 5 run で `plan` tool を invoke せず、
直接 text-reply attractor に落ちた。 prompt は複数ファイル読み込み + 合成を要求する
設計 (「src/reyn/ 以下の Python ファイルそれぞれを読んで、各ファイルの設計意図と役割を
2 段落で詳しく説明して」) だが、 LLM は plan tool を使わず限定的な reyn_src_read
(または training-data knowledge のみ) で応答した。

### A2A 制約下での crash 観測の根本的限界

S3 の本来の目的は「crash + auto-resume + memo replay」の観測だが、
A2A 制約下では以下の理由でこれを観測できない:

**限界 1: kill -9 不可**
- 真のプロセスクラッシュは web server の SIGKILL を必要とする。
- Web server は S1/S2/S4/S5 の 4 エージェントが同時利用中のため
  kill -9 は禁止 (= 他エージェントのセッションが失われる)。
- 結果として `plan_resume_coordinator` の adopt/cancel 判定は
  今回の N=5 では一度も実行されなかった。

**限界 2: per-process singleton のセッション分離**
- Reyn の chat session は per-process singleton として動作する。
- A2A の 2nd request は同じセッション上のキューに積まれるため、
  「process restart → auto-resume」という流れを模擬できない。
- HTTP timeout abandon (= 8s) は client-side のみで完結し、
  server-side session は継続実行する。 「プロセス再起動なしの auto-resume」は
  トリガーされない。

**限界 3: plan not invoked → 観測対象が存在しない**
- plan snapshot は `plan_started` WAL event で生成される。
- plan が一度も invoke されなかったため、
  plans/ ディレクトリが作成されず、 snapshot ファイルも生成されなかった。
- `record_plan_started`, `record_plan_completed`, `plan_completed` cleanup
  のいずれも WAL に記録されていない。

### 何が観察できたか

1. **HTTP timeout abandon の server-side 挙動**: 8s timeout 後もサーバーは処理を継続
   し、 history.jsonl に `user` + `agent` エントリを書き込んだ (Run 2/3 の seq 5, 6, 7)。
   これはキューの flush 完了を確認できる観測証拠。

2. **clean_state() の history.jsonl wipe 動作**: B16-S1-1 修正後の driver では
   history.jsonl も wipe される。 Run 1/5 の follow-up では「どの作業ですか？」という
   コンテキスト喪失返答が確認でき、 wipe が機能していることを間接確認できた。

3. **history bleed race condition (B16-S3-1)**: Run 2/3 で abandon→15s wait→
   clean_state() のシーケンス中に、 server-side 処理が history.jsonl に書き込みを
   継続する場合がある。 clean_state() タイミングによっては wipe 後に別 run の
   server-side agent entry が書き込まれる可能性が残る (= Run 4 の follow-up 挙動)。

4. **plan_resume_coordinator のコード存在確認**: `src/reyn/plan/` 以下に
   `plan_resume_coordinator.py`, `plan_resume_analyzer.py`, `plan_snapshot.py`
   が存在し、 PlanSnapshot の lifecycle コメント (plan_started → step_completed →
   plan_completed/aborted) が実装されていることをコードレビューで確認。
   ただし A2A 制約下での E2E 観測には至らない。

---

## 4. 何を意味するか

### S3 シナリオの観測可能範囲の再定義

S3 crash + resume は **E2E 観測不可** (= A2A + plan not invoked のダブルブロック)。
S1 が "plan invoked か" を問い、 S3 は "plan invoked 後に crash 耐性があるか" を問う。
S1 の refuted 5/5 が継続する限り、 S3 は観測対象ゼロになる構造的必然がある。

### 将来の S3 観測には別ハーネスが必要

本テストで判明した観測の前提条件:

1. plan tool が invoke されること (= B16-S1-2 の修正、 または prompt engineering)
2. process-level crash を安全に発火できること

オプション A: **dedicated subprocess harness**
- S3 専用 web server プロセスを subprocess で起動し、 SIGKILL で強制終了。
- 他エージェントと干渉しない独立 port で稼働。
- 起動→ plan trigger → SIGKILL → 再起動 → resume 観測 の完全サイクル。

オプション B: **unit-level ChatSession crash simulation**
- `ChatSession` に `simulate_crash()` メソッドを注入し、
  plan 実行中に WAL write 後 / snapshot 後の任意の点で強制例外。
- E2E ではなく white-box。 resume 判定ロジックの unit test として有効。

オプション C: **direct WAL state write (forensic)**
- 実行せずに手動で `plan_started` + `plan_step_completed` WAL エントリを書き込み、
  resume coordinator の判定をドライ観察。
- 実行時挙動 (network タイミング等) は観測できないが、
  resume policy の正確性は確認可能。

### B16-S1-2 修正が S3 観測の前提

plan tool が invoke されるようになれば、 S3 の観測は再挑戦できる。
ただし process crash の制約は別途解決が必要。 S3 の真の観測は batch 17 以降に回す。

---

## 5. 新規バグ

### [MED] B16-S3-1: history bleed race condition (abandon + clean_state)

| 項目 | 詳細 |
|---|---|
| ID | B16-S3-1 |
| 重要度 | MED (= dogfood 観測精度に影響、 本番ではコンテキスト汚染) |
| 現象 | HTTP timeout abandon 後に server-side LLM 処理が継続し、 clean_state() が history.jsonl を wipe しても、 その後に server-side agent entry が追記される場合がある |
| 証拠 | Run 4 の follow-up reply が「src/reyn/ を再検討した」と先行 run 内容を参照した挙動 (= clean_state() 後に Run 3 server-side entry が書き込まれた可能性) |
| 影響 | 連続 N=5 run で独立性が確保されない。 client が abandon した turn の agent reply が次 run の history に混入する場合がある |
| 修正候補 | clean_state() 後に一定時間 (≥5s) の wait を挟む、 または server-side 処理完了を polling で確認してから clean_state() を呼ぶ |
| scope | driver.py の run_one() 関数に post-clean wait を追加 |

---

## 6. Calibration Delta

S1/S2 の観測結果を踏まえ、 事前に予測を建てると:

| 予測 | 実測 | Brier component |
|---|---|---|
| verified 5% | 0/5 (0%) | (0.05-0)² = 0.0025 |
| inconclusive 10% | 0/5 (0%) | (0.10-0)² = 0.0100 |
| refuted 80% | 5/5 (100%) | (0.80-1.0)² = 0.0400 |
| blocked 5% | 0/5 (0%) | (0.05-0)² = 0.0025 |
| **Brier score** | — | **0.0550** (= 4 class 平均: 0.0138) |

S1 の Brier 0.70 (事前予測の大幅外れ) を受けて、 S3 では refuted 率を大幅に
引き上げた想定予測。 実測と整合しており、 S1 観測からの calibration 更新は有効だった。

### S3 特有の calibration 知見

- plan not invoked → crash-resume 観測ゼロ は論理的必然。
  S3 固有の「crash 耐性」を評価するには、 まず S1/S2 の G1 (plan invocation) が
  解決される必要がある。
- A2A 制約下での crash 観測の根本的限界は、 dogfood harness の設計上の穴として
  今後の batch 設計に組み込む必要がある。

### 次回 S3 観測 (batch 17+) の推奨構成

1. B16-S1-2 fix (plan tool invocation の修正) を先に着地させる
2. dedicated subprocess harness を用意する
3. S3 専用の plan-triggering prompt を使用する (= 単一 long prompt ではなく
   「意図的に multi-step synthesis が必要な設計」の prompt)
4. N=5 の各 run で SIGKILL + 再起動 + resume を完全サイクルで観測する
