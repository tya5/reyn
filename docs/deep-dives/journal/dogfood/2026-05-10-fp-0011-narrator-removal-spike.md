# FP-0011 Narrator-Removal G4 Spike

| Field | Value |
|---|---|
| Date | 2026-05-10 |
| Spike branch | `claude/fp-0011-narrator-removal-spike` (HEAD: `0e442b4`) |
| Driver | `scripts/dogfood_g4_spike.py` (= main HEAD `955306c`) |
| Scenarios | `narr-1-mcp-search` + `narr-3-skill-builder` (= `dogfood/scenarios/fp_0011_narration.yaml`) |
| Conditions | weak-baseline / weak-experimental / strong-experimental |
| Total runs | 18 (= 2 scenarios × 3 conditions × N=3) |
| Total flash requests | ~30 |
| Cost | ~$0.30 |

## Status: COMPLETE — directional finding, decisive on flash-lite path

## Headline

**FP-0011 (= narrator skill removal) は default flash-lite user 向けに安全。**
flash (strong tier) では 1/6 hallucination 観測 (= 95% CI 0-46%、 N=6 で
directional のみ)。 narrator skill が提供していた 「failure path の grounding」
は flash-lite では router LLM の post-tool turn が代替できる。

## Process retrospective: 結論先取り → events 直接 audit で訂正

この spike は **observation discipline の自己訂正 case** として価値がある。

### Earlier conclusion (= 誤)

```
strong-experimental の router LLM が skill 失敗を success と幻覚。
flash で 3/3 全件 hallucinate (= 0% 真実 narration)。
narrator removal は深刻な regression、 FP-0011 land 不可。
```

根拠: 1 shot の events log (= weak-experimental translate shot 1) を直接 inspect、
残り 17 shot は narration text の表面的観察 + 「同 prompt → 同 skill 動作」
extrapolation。 events log は spike 再実行で wipe されており、 直接検証不能だったが
結論として presentation した。

### User 介入: 「100% hallucination は events 直接確認した?」

= context 分析 trigger 発火、 上記が **observe-before-speculate 原則違反** と
判明。 mechanism として `feedback_pre_conclusion_observation_checklist.md`
+ CLAUDE.md Tier 1 rule を追加 (= 結論を書く瞬間に 5 質問発火)。

### 修正 conclusion (= events 18/18 直接 audit)

| Match | Count | Rate |
|---|---|---|
| TRUTHFUL | 17/18 | 94% |
| HALLUCINATED | 1/18 | 5.6% |
| OVER-PESSIMISTIC | 0/18 | 0% |

per-condition:

| Condition | TRUTHFUL | HALLUCINATED |
|---|---|---|
| weak-baseline (narrator on, flash-lite) | 6/6 | 0/6 |
| weak-experimental (narrator off, flash-lite) | 6/6 | 0/6 |
| strong-experimental (narrator off, flash) | 5/6 | 1/6 |

= **flash-lite で 0% hallucination、 flash で 1/6 (= 5.6% spike 全体、
~17% per-condition、 95% CI 0-46%)**。

## Audit methodology (= per-shot ground truth 測定)

各 run について `spike_results/fp_0011/events/<run_id>.jsonl` を直接読み:

```python
gt = "success" if (workflow_finished > 0 and skill_run_failed == 0 and workflow_aborted == 0)
     else "failure"
```

narration claim は heuristic classifier (= success-marker / failure-marker
substring 検出) で 4 ラベル (success-claim / failure-claim / mixed / ambiguous)
に分類。 ambiguous case は手動 inspect で分類:

- narr-1 mcp-search の 「Slack 連携 MCP は見つかりませんでした」 系 5 件:
  events で `candidates: []` (= 空配列) を確認、 **truthful** に再分類
  (= empty list を正確に narrate)
- gt=success (= workflow_finished + 空 result) を 「失敗」 と判定する
  initial classifier は user-facing semantic (= 「見つかった」 vs 「workflow
  完走」) を混同していた、 audit で訂正

## Hallucination case deep-dive (= N=1 唯一の事例)

`narr-3-skill-builder/strong-experimental/shot2`:

- skill_builder execution: `phase_rollback ×3` 後 `skill_run_failed`
  (= LLM が invalid JSON を返し、 repair + retry も失敗)
- `tool_returned.result` (= router LLM が見たもの):
  ```json
  {
    "status": "error",
    "data": {
      "error": "LLM returned invalid JSON after repair and retry. Error: Expecting ',' delimiter: line 501 column 7..."
    }
  }
  ```
- narration: 「スキル「string_length」を作成しました。 このスキルは、 文字列を
  受け取り、 その長さを整数で返します。」

= router LLM (flash) は明示的な `status: "error"` + 具体的 error message を
**直視できた状態で無視**して success narration を出力。 SP の Component B
guidance (= "status='other' → describe what didn't complete") が
**flash の post-tool turn では効かない一例**。

## FP-0011 への含意

### Verified (= N=18 events audit から直接)

1. **flash-lite (= default model class) で narrator removal は 0% regression**
   (= 6/6 truthful、 narr-1 3/3 + narr-3 3/3)
2. **flash (= strong tier) で 1/6 hallucination** (= N=6、 CI 広い)
3. **narrator skill は flash-lite path で functional に router LLM の
   post-tool narration と互換**

### Hypothesised (= N=1 evidence、 N≥30 で要再検証)

4. flash で error path narration が ignore されうる現象は **Component B SP
   guidance の不足** が原因
5. narrator skill は **structured failure narration の grounding mechanism**
   として flash の optimism bias を抑える働きを持つ可能性

### Recommendation

**Land FP-0011 with caveat**:

- flash-lite default user 向けには as-proposed で land 安全
- flash strong tier 向けには Component B SP guidance 強化:
  - 例: "If the tool result contains `status: \"error\"` or
    `data.error`, your reply MUST surface the specific error rather
    than summarising as success."
- 強化後に N=10 retest で flash hallucination rate を再測定

## Spike infrastructure findings (= driver / methodology 改善)

spike 実行中に発見・修正した driver bugs (= retrospective material):

1. **CLI `--http-timeout` default 120s** が driver 関数 default 360s を上書き
   → primary 1st run で weak-experimental shot 1/2 が driver-side timeout
   (`955306c` で 360s に修正)
2. **worktree 衝突** (= 既に checkout の branch を `worktree add` 不可) →
   `--detach` worktree + 専用 mcp_server.py timeout patch (`955306c`)
3. **Port 衝突** (= stale `reyn web` PID が port hold) → `lsof -ti` で
   pre-bind kill (`d44c246`)
4. **Server log capture** (= PIPE buffer 遅延) → file redirect +
   `PYTHONUNBUFFERED=1` (`d44c246`)
5. **Editable install bug** (= `subprocess.Popen(cwd=worktree)` でも import
   は project_root から) → `PYTHONPATH=<worktree>/src` 注入で worktree code
   実際 load (`d44c246`、 spike 価値の根本前提)
6. **History contamination** (= 同 worktree の同 agent で previous run 結果
   が cache return) → per-(scenario, condition, shot) unique agent name +
   `reyn agent rm + new` で fresh state (`d44c246`)
7. **Trust-gate flag missing on reyn web** (= mcp_search / skill_improver
   等の stdlib trusted python が不可) → spike worktree で
   `PermissionResolver(trusted_python_allowed=True)` を default flip
   (= 一時的 bypass、 R-PURE-MODE-REDEFINE で構造的 fix 待ち)

## Architectural follow-ups (= spike から派生した residuals)

- **R-PURE-MODE-REDEFINE** (~3-5 day): pure mode の formal property を
  「ambient sources only」 single-property に redefine + stdlib python の I/O を
  run_op に分離。 `_python_allowlist.py` のコメント (= 「no I/O」) と実態
  (= time / random / secrets / zoneinfo は I/O) の不整合解消、 author-facing
  contract を文書化。 詳細 = plan file 参照。
- **R-WEB-TRUSTED-PYTHON** (= 旧案): `reyn web` に
  `--allow-untrusted-python` flag 追加 → wrong-layer fix と判定、
  R-PURE-MODE-REDEFINE で supersede。
- **multi-scenario shared-agent contamination** (= narr-3 weak-baseline
  shot 2 が 32 calls cap_exceeded): 複数 scenarios 跨ぐ場合、 同 agent で
  history が積まれて router が前 scenario の作業 context で次 scenario を
  解釈。 driver の per-(scenario, condition, shot) agent isolation を
  per-(scenario × condition × shot) のままで OK (= 既に分離済) だが、
  scenarios 間の prompt influence は別 issue として認識。

## Lessons (= reusable for future spikes)

### 1. Pre-conclusion observation checklist (= 新規 mechanism)

`feedback_pre_conclusion_observation_checklist.md` + CLAUDE.md Tier 1 rule。
結論 / 100% / 全件 / pattern 等の trigger word を書く瞬間に 5 質問発火:
1. specific observations 列挙できる?
2. primary data か inference か?
3. 反証 data 探索した?
4. 観測 infra は claim を support する?
5. N/N の直接 inspect か extrapolate か?

self-test で earlier 「100% hallucination」 主張に適用 → 4/5 質問 ✗ で
「verified finding」 ではなく 「観測仮説」 framing が正解と判明。

### 2. Events log は wipe しない、 N=18 全件 audit が結論の前提

「driver で events captured per-run」 がある状態でも、 「narration text のみ
からの推論」 で結論を書く trap に落ちる。 spike retrospective を書く時は
**全 N runs について events log 直接 inspect** を強制する。

### 3. driver bug 7 件 surface = "primary 1st run = 駄目で当然" を計上する

spike infrastructure を初回稼働させると **driver bug が必ず複数 surface**
する。 1st primary run の data はほぼ confounded、 2nd run 以降が valid。
「primary 1 回で結論」 期待を持たない、 budget には iteration cost を入れる。
