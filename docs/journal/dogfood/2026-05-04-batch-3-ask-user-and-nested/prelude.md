# Batch 3 (ask-user-and-nested) — Prelude

> batch 2 で HIGH 3 件が全 fix された状態で、 batch 2 では観測しきれなかった
> 経路 (ask_user IR op / nested skill) と HIGH fix 後の再確認 (multi-agent)、
> および open のままの MED 2 件 (narrator / hallucination) を中心に組む。

## 2026-05-04 夜、 reyn の戸籍

- **main HEAD**: `8f417a5` (= test(budget): Tier 2 rate-limit window invariants)
  batch 2 から以下の fix が landing 中 (並列 wave で進行):
  - `83bad83` / `e9216f6`: fix(chat): B2-H1/H2 (specialist attractor + silent absorption)
  - `a75587d`: fix(examples): B2-H3 (with-mcp.yaml permission 行)
  - `d9e5fce`: fix(cost): F4 residual (router_model key for pricing lookup)
  - `8f417a5`: test(budget): R-D8 supplement (rate-limit window invariants)
- **全 test**: 642 passed (最終確認コミット `8f417a5` 時点)
- **open MED/LOW**: B2-M1 (hallucination) / B2-M2 (tool_failed 英語) /
  B2-M3 (MCP teardown) / B2-M4 (narrator) / B2-L1〜L3 (polish)

batch 2 完了時の結論:

> regression net は green。 ただし multi-agent UX には attractor 形 HIGH bug
> が 2 件残る。 カレーレシピが user に届く日は、 まだ来ていない。

batch 3 開始時点でその 2 件 (B2-H1+H2) は fix 済み。
カレーレシピが届くかどうかが最初の観測対象になる。

## batch 2 → 3 の間で何が起きたか

| Commit | 内容 | finding 対応 |
|---|---|---|
| `83bad83` | fix(chat): peer-failure surfacing (B2-H1+H2 差分込み) | B2-H1 + B2-H2 |
| `e9216f6` | fix(chat): deterministic peer-failure surfacing 追補 | B2-H2 |
| `a75587d` | fix(examples): with-mcp.yaml permission 行追加 | B2-H3 |
| `d9e5fce` | fix(cost): router_model key で pricing lookup | F4 residual |
| `2e72b4c` | docs(readme): OSS public release 向け rewrite | (docs) |
| `8f417a5` | test(budget): rate-limit window Tier 2 invariants | (test) |

**open のまま (Wave B へ)**: B2-M1〜M4 / B2-L1〜L3。

## 当時の事前仮説 (scenario ごとの prediction 集約)

### S1: multi-agent re-confirm (当たり期待 70%)

B2-H1+H2 の fix はコード変更済み + Tier 2 green。 しかし gemini-2.5-flash-lite
が `describe_skill` 後に `invoke_skill` を呼ぶ prompt rule を honor するかは
LLM attractor 依存。 最も楽観的な予想は「カレーレシピが届く」。

外し方の想定: fix が効いて specialist が invoke できるようになっても、
`direct_llm` skill の LLM 呼び出し結果が narrator → default → user の
chain 接続で詰まる新パターンが出る。

### S2: ask_user e2e (当たり期待 40%)

B2-INFO の教訓を受けて skill 名明示 + path 曖昧の組み合わせで設計。
ただし弱モデルが `ask_user` IR op を発行する判断をするかどうかが不確実。
「ファイルが無ければ報告して終わる」 パターンに逃げる可能性が高い。

外し方の想定: router が再び pre-skill clarification に逃げて skill 未起動
(B2-INFO と同じ結末)。 または skill は起動するが LLM が `abort` を選択。

### S3: nested skill (当たり期待 35%)

最も不確実。 `eval_builder` が `run_skill` IR op を実際に使う設計かどうか、
run_skill op の OS 実装が e2e で機能するかどうかが未知数。
初回観測のため「何かが分かる」だけで成功と見なす。

外し方の想定: eval_builder が run_skill を使わず直接 LLM 処理する設計で
nested 経路が発火しない setup 問題。 または OS の run_skill dispatch が
未完装で NotImplementedError が出る。

### S4: narrator 品質 (当たり期待 45%)

B2-M4 は batch 3 時点 open。 修正されていなければ 2-turn 再現。
gemini-2.5-flash-lite が skill output を narrator context に拾う可能性は
model attractor 次第で 45% 程度。

外し方の想定: 2-turn は継続するが 2 turn 目の内容の質が改善 (partial improvement)。
または 1 turn 成功でも「何の project か」 への回答が抽象的すぎる。

### S5: skill 名 hallucination (当たり期待 30%)

B2-M1 は batch 3 時点 open。 router prompt に「まず list_skills」 指示が
追加されていなければ高確率で再現。 list_skills が呼ばれても hallucination が
完全消滅するとは限らない。

外し方の想定: list_skills は呼ばれるようになったが `text_summarizer` が
`reyn/local/` (gitignored) にあって list に出ず → `direct_llm` を選択して
成功という partial improvement。 B2-M2 (英語 fallback) の連動確認も。

## 観測体制の改善点 (batch 2 で見落とされた F2/F4/F8 への反省)

batch 2 の後追い verification で F2/F4/F8 が「scenario 設計外だったので
直接観測できなかった」 経緯を踏まえ、 batch 3 では以下を全 scenario で
標準観測に含める:

| 追加観測項目 | なぜ | batch 2 での失敗 |
|---|---|---|
| `:cost` 確認 | cost 0 が続いていないか (F4 教訓) | F4 residual が後追いまで気づかれなかった |
| fallback path の言語 | tool_failed 後も output_language=ja か (B2-M2) | F11 の適用範囲確認が正常経路のみだった |
| MCP teardown stderr | `cancel scope` RuntimeError が出ないか (B2-M3) | batch 2 で stderr 確認を忘れて後追い |
| `rm -rf .reyn/` で reset | state 残留が影響しないよう (B2-L3 教訓) | `rm -rf .reyn/state .reyn/events` が不完全 |
| skill_runs エントリ直接確認 | skill が本当に起動したかを log から確認 | WAL grep を後追いで実施していた |

## 観測ポイントのフォーマット変更

batch 2 まで: 観測ポイントを「何を見るか」 の記述のみ。
batch 3 から: 各観測ポイントに **具体的な grep / コマンド** を添付。
「WAL grep: `grep skill_phase_advanced .reyn/events/chat/*.jsonl`」 形式で
実行可能な確認方法を記述。 これにより観測漏れを防ぐ。

## batch 3 を終えたとき、 何が分かるべきか

1. **カレーレシピが届いた/届かなかった** (= HIGH fix の e2e 確認)
2. **ask_user IR op の発火経路が e2e で動作するか** (= B2-INFO の batch 3 再設計が機能したか)
3. **nested skill は OS レベルで接続しているか** (= 初観測、 動く/動かない)
4. **narrator 品質の現状** (= B2-M4 の regression 確認または partial improvement)
5. **hallucination が list_skills で抑制されるか** (= B2-M1 の regression 確認)

これらが記録できれば batch 3 は成功。 HIGH が 0 件なら「OS core は安定した」
という評価に近づく。 HIGH が出るなら「multi-agent chain の深部にまだ穴がある」
という結論に。

---

## 関連 doc

- [batch 2 findings.md](../2026-05-04-batch-2-real/findings.md) — 前 batch summary
- [B2-INFO](../2026-05-04-batch-2-real/findings/B2-INFO-ask-user-not-observed.md) — ask_user 観測不能の経緯と batch 3 向け再設計
- [B2-H1](../2026-05-04-batch-2-real/findings/B2-H1-specialist-describe-invoke-fail.md) — specialist attractor (fixed)
- [B2-H2](../2026-05-04-batch-2-real/findings/B2-H2-default-silent-absorption-marker.md) — silent absorption (fixed)
- [B2-M4](../2026-05-04-batch-2-real/findings/B2-M4-narrator-generic-completion.md) — narrator 2-turn 問題 (open)
- [B2-M1](../2026-05-04-batch-2-real/findings/B2-M1-router-hallucinates-skill-name.md) — hallucination (open)
