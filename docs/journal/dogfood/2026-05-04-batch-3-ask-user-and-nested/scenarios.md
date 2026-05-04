# Batch 3 (ask-user-and-nested) — Scenarios

> batch 2 で観測しきれなかった経路 + HIGH 3 件修正後の再確認。
> 重点: ask_user IR op e2e / nested skill (run_skill) / multi-agent re-confirm /
> narrator 品質 / skill 名 hallucination 改善確認。 5 scenario。

## 共通前提

- LLM: LiteLLM proxy at `localhost:4000`、 model `openai/gemini-2.5-flash-lite`
- 実行 dir: `~/Workspace/junk/claude_sandbox/sandbox_2`
- CUI mode (`--cui --no-restore`) で記録、 観測ポイントに WAL / events grep を含む
- batch 開始前 `rm -rf .reyn/` で完全 state flush (= B2-L3 教訓)
- main HEAD: `8f417a5` (B2-H1/H2/H3 全 fixed 後)

## 観測の枠組み (6 軸、 batch 1/2 と同じ)

| 観点 | 何を見るか |
|---|---|
| **応答品質** | LLM の文章が用件に合ってるか、 日本語として自然か |
| **意図解釈** | router / agent の routing が user 意図に沿うか |
| **待ち時間** | 1 turn の応答までの秒数。 会話テンポが成立するか |
| **見せ方** | 内部状態 (phase / chain / agent name) の露出が適切か |
| **エラー UX** | 失敗時の文言が user にとって actionable か |
| **state 整合性** | events / WAL / outbox が想定通りに進むか |

## batch 3 追加観測リスト (batch 2 で見落とした F2/F4/F8 の教訓)

- `:cost` を各 scenario で確認 (F4 residual 教訓: cost 0 が続いていないか)
- `skill_runs/` エントリの有無を direct grep で確認
- fallback path: `tool_failed` 後の inbox message body の言語を確認 (B2-M2)
- MCP teardown: stderr に `cancel scope` RuntimeError が出ないか確認 (B2-M3)

## 構成

| ID | 種別 | カバー領域 | 期待 |
|---|---|---|---|
| S1 | HIGH 再確認 | B2-H1/H2 fix 後の multi-agent re-confirm | カレーレシピが届く |
| S2 | new | ask_user e2e (IR op 発火) | ask_user prompt が CUI に出る |
| S3 | new | nested skill (run_skill IR op) | 親→子 skill chain が接続 |
| S4 | MED 確認 | narrator 品質 (B2-M4 2-turn 問題) | 1 turn で skill 出力が含まれる |
| S5 | MED 確認 | skill 名 hallucination (B2-M1) — list_skills 必須化の効果 | 正しい skill 名で invoke |

---

## Scenario 1 (HIGH re-confirm): multi-agent — カレーレシピが届くか

### 目的

B2-H1 (specialist の describe→invoke attractor) と B2-H2 (default の
`_no_reply_marker` silent absorption) が修正された。 batch 2 では
「かしこまりました」で終わったカレーレシピが、 今度こそ user に届くかを確認。

### Setup

```bash
rm -rf .reyn/
reyn chat default --cui --no-restore
```

specialist agent が `_default` topology に存在することを topology で確認してから実行。

### Action

```
"specialist エージェントに「カレーの簡単な作り方」 を聞いて教えて"
```

### 期待結果

- default が `delegate_to_agent` で specialist に **1 回だけ** dispatch
- specialist の RouterLoop が `invoke_skill` (または直接 reply) まで進む
  (= list_skills → describe_skill → invoke_skill の describe 止まりにならない)
- default が `_no_reply_marker` を受け取った場合は「specialist から結果が得られませんでした」と user に伝える
  (= B2-H2 fix の効果)
- 理想: カレーのレシピが user に届く

### 観測ポイント

- WAL grep: `skill_phase_advanced` が specialist 側に出るか
  (`grep skill_phase_advanced .reyn/events/chat/*.jsonl`)
- WAL grep: `agent_message_sent` の前に `invoke_skill` が呼ばれているか
  (`grep tool_called .reyn/events/chat/*.jsonl | grep invoke_skill`)
- WAL grep: `peer_reply_failed_surfaced` event が出る場合 (= H2 fix path を通ったか)
- CUI: 「かしこまりました」 で終わるなら B2-H2 fix が機能していない
- CUI: カレーレシピが出れば両方 fix が効いている
- `:cost` で cost > 0 か確認 (F4 教訓)

### 事前 prediction

**当たり期待: 70%** — H1+H2 fix はコード変更済みで Tier 2 test も green。
ただし弱モデル (gemini-2.5-flash-lite) が新 prompt rule を honor するかは
LLM attractor 次第。 specialist が describe で止まる attractor が prompt 強化で
消えているか、 それとも再出現するかが観測の核心。

**外れ予測として意識する点**: fix が効いて specialist が invoke できるようになったとしても、
specialist が `direct_llm` skill を選択しても、 その skill の LLM 呼び出し結果が
narrator を経由して default → user に届く chain 接続で別の問題が出る可能性がある。
「レシピは生成されたが user に届かない」 という新パターンで外れる可能性を意識する。

---

## Scenario 2 (new): ask_user e2e — IR op が CUI に届くか

### 目的

B2-INFO で判明した通り、 S4 (batch 2) では skill 起動前に router が pre-skill
clarification を行い `ask_user` IR op が発火しなかった。 batch 3 では
skill 名を **明示指定** し、 かつ **skill 内部で path が曖昧** になる状況を
作ることで、 IR op 経路を強制的に通す。

### Setup

```bash
rm -rf .reyn/
reyn chat default --cui --no-restore --config examples/configs/with-mcp.yaml
```

`report.md` は実在しないファイル (= skill 内部の path resolution が失敗し
ask_user を発行する状況を誘発)。

### Action

```
"read_local_files skill を使って report.md を読んで要約して"
```

(= skill 名明示 + path 曖昧の組み合わせ)

user が ask_user prompt に対して回答するターン (= 実行 agent が中断して回答を待つ
段階):

```
"ファイルが見つかりません。 どのファイルを読みますか？" → "README.md を読んで"
```

### 期待結果

1. router が `read_local_files` を invoke (skill 名明示なので確実に起動)
2. skill phase の LLM が `ask_user` IR op を発行
   (= `report.md` が存在しないので path 確認を要求)
3. CUI に clarifying question が表示される (intervention_dispatched event)
4. user が `"README.md を読んで"` と回答
5. skill が README.md を read → 要約 → narrator が完了応答

### 観測ポイント

- WAL grep: `intervention_dispatched` が出るか
  (`grep intervention_dispatched .reyn/events/chat/*.jsonl`)
- WAL grep: `intervention_resolved` が出るか (user 回答後)
- CUI: clarifying question の文言が日本語かつ具体的か
- WAL: `skill_started` → `intervention_dispatched` → `intervention_resolved`
  → `skill_completed` の順序が成立しているか (out-of-order は state 不整合)
- `:cost` で ask_user → resume の multi-turn cost が蓄積されているか

### 事前 prediction

**当たり期待: 40%** — ask_user IR op の dispatch 自体は Tier 2 で pin 済みだが、
`report.md` 不在で skill の LLM が `ask_user` を発行するかどうかは LLM 判断次第。
弱モデルは「ファイルが無ければ諦める」か「報告して終わる」パターンに行く可能性がある。

**外れ予測として意識する点**: router が skill 名を明示されても再度 pre-skill
clarification を挟む (B2-INFO と同じ挙動で skill 未起動) という失敗パターンと、
skill は起動するが LLM が `ask_user` でなく `abort` を選んで終わるパターンを区別して記録する。

---

## Scenario 3 (new): nested skill — run_skill IR op の chain 接続

### 目的

batch 2 scenarios.md の `defer (batch 3 へ)` に明記された「nested skill
(= run_skill IR op、 parent → child)」 を初観測。 あるスキルが別スキルを
`run_skill` IR op で呼び出し、 child skill の output が parent skill の
workspace に届くかを確認。

### Setup

```bash
rm -rf .reyn/
reyn chat default --cui --no-restore
```

`eval_builder` skill は内部で `eval` skill を `run_skill` で呼ぶ設計に
なっているか確認 (skill.md を事前に読んで run_skill op の有無を確認)。
もし `eval_builder` が run_skill を使わない設計なら `skill_builder` +
`judge_phase` の組み合わせを試す。

### Action

```
"eval_builder skill を使って、 次の Python 関数の正しさを評価するテストケースを 2 件作って:
def add(a, b): return a + b"
```

(= eval_builder が eval を sub-skill として呼び出す経路を狙う)

### 期待結果

- router が `eval_builder` を invoke
- `eval_builder` skill の中で `run_skill("eval", ...)` IR op が発行される
- OS が child skill `eval` を起動、 実行後 output を parent workspace に返す
- parent (`eval_builder`) が child output を受け取り、 最終 output を生成
- narrator が user に評価結果を提示

### 観測ポイント

- WAL grep: `sub_skill_started` / `sub_skill_completed` event が出るか
  (`grep sub_skill .reyn/events/chat/*.jsonl`)
- WAL: parent skill の workspace に child output の artifact が書き込まれているか
  (`cat .reyn/state/skill_runs/*/workspace/*.json | jq .`)
- WAL: `run_skill` op が `control_ir` に出現しているか
  (`grep run_skill .reyn/events/chat/*.jsonl`)
- skill_runs に 2 エントリ (parent + child) が出るか
  (`ls .reyn/state/skill_runs/`)
- `:cost` で nested 呼び出し分の LLM cost が記録されているか

### 事前 prediction

**当たり期待: 35%** — `eval_builder` が実際に `run_skill` を使う設計かどうかが
不確実。 使わない設計なら経路が通らず、 skill 自体が直接 eval 処理をする可能性がある。
また run_skill IR op の OS 実装が完全かどうかの e2e 確認は初回。

**外れ予測として意識する点**: eval_builder が run_skill を使わず直接 LLM で処理する
設計の場合、「nested skill が動かない」ではなく「nested skill を使う skill が無い」
という setup 問題として記録する。 その場合 skill.md を精査して代替 skill を探す
後続アクションを取る。

---

## Scenario 4 (MED confirm): narrator 品質 — skill 出力が 1 turn で届くか

### 目的

B2-M4 で観測した「narrator が `完了しました` だけ言って skill 出力を提示しない」
2-turn 体験を再現確認。 batch 3 時点で修正されていれば 1 turn で内容が届く。
未修正なら再現を記録して severity を再評価する。

### Setup

```bash
rm -rf .reyn/
reyn chat default --cui --no-restore --config examples/configs/with-mcp.yaml
```

B2-M4 と同じ再現手順を使う (= `read_local_files` で README.md を読む)。

### Action

```
"read_local_files skill を使って README.md を読んで、 何の project か 1 段落で説明して"
```

### 期待結果

- router が `read_local_files` を invoke
- skill が README.md を読取り
- narrator が **1 turn 目** に README.md の内容を含む説明を返す
  (「このプロジェクトは...」 のような具体的な記述が含まれる)
- 「スキルが正常に完了しました」 のみで終わらない

### 観測ポイント

- CUI: 1 turn 目の reply に README.md の内容 (Reyn、 workflow、 skill 等の
  キーワード) が含まれるか
- CUI: 「reyn run <skill_name>」 等の internal な CLI 指示が reply に滲まないか
- WAL grep: `skill_completed` event が 1 件か、 narrator の LLM 呼び出しが
  skill output を context に持っているか (events で確認)
- 2 turn 必要なら B2-M4 open を再確認記録、 1 turn なら fix または自然改善と記録

### 事前 prediction

**当たり期待 (1 turn 成功): 45%** — B2-M4 は batch 3 時点で open のまま。
修正されていない前提なら 2-turn 体験が再現する可能性が高い。
ただし narrator の final_output 伝達は LLM prompting 次第なので、
gemini-2.5-flash-lite が今回は skill output を拾う可能性もある。

**外れ予測として意識する点**: 2 turn になるとしても、 2 turn目の内容が前回より
具体的かどうかを観測する。 「完了→詳細」 の 2-turn が「詳細→補足」 に変わって
いれば improvement、 前回と同一なら完全 regression として記録する。

---

## Scenario 5 (MED confirm): skill 名 hallucination — list_skills 効果確認

### 目的

B2-M1 で観測した「router が `list_skills` を使わず `general.summarize` を
hallucinate する」 問題。 router prompt への「まず list_skills を呼べ」 指示追加
(B2-M1 修正候補) が反映されていれば hallucination が減る。 未修正なら再現を記録。

### Setup

```bash
rm -rf .reyn/
reyn chat default --cui --no-restore
```

B2-M1 と同じ入力で、 router の tool_call sequence を観測する。

### Action

```
"次の英文を 3 つの bullet point に要約して: Python is a high-level programming
language created in 1991 by Guido van Rossum. It emphasises code readability
and supports multiple programming paradigms."
```

### 期待結果 (修正済みの場合)

- router が `list_skills` を呼んで available skills を確認
- 利用可能なスキル一覧から適切な skill を選択 (= `text_summarizer` が
  `reyn/local/` に存在するなら選択、 なければ `direct_llm` 等)
- `general.summarize` のような存在しない skill 名を invoke しない
- `invoke_skill` の `name` フィールドに実在する skill 名が入る

### 期待結果 (未修正の場合)

- WAL: `tool_failed` + `invoke_skill` → `general.summarize` が再現
- 直後の fallback reply が英語で出る (B2-M2 も連動)

### 観測ポイント

- WAL grep: `tool_called` の sequence で `list_skills` が先に呼ばれているか
  (`grep tool_called .reyn/events/chat/*.jsonl | grep list_skills`)
- WAL grep: `tool_failed` が出るかどうか (`grep tool_failed .reyn/events/chat/*.jsonl`)
- WAL: `invoke_skill` の `name` フィールド (= 実在するか確認)
- CUI: fallback reply の言語 (= B2-M2 連動確認、 英語なら open)
- `:cost` で cost が記録されているか (F4 教訓)

### 事前 prediction

**当たり期待 (hallucination なし): 30%** — B2-M1 は batch 3 時点で open のまま。
router prompt の「まず list_skills」 指示が未追加なら高確率で再現する。
list_skills が呼ばれても hallucination が完全消滅するとは限らない
(= LLM が list に無い名前を推測するパターンが残る)。

**外れ予測として意識する点**: list_skills を呼ぶ改善がされていれば hallucination が
減るが、 `text_summarizer` が `reyn/local/` (gitignored) にある場合は
list に出てこない可能性がある。 その場合 router が代替 skill (`direct_llm`) を
選択して成功するという「hallucination は無いが期待 skill も使わない」 中間結果
になるかもしれず、 それは partial improvement として記録する。

---

## 事前 prediction 集約

| ID | 当たり期待 | 外れた場合の典型パターン |
|---|---|---|
| S1 | 70% | fix は効いたが chain 接続で新問題 (カレー届かず) |
| S2 | 40% | router が pre-skill clarification に逃げて IR op 未発火 |
| S3 | 35% | eval_builder が run_skill を使わない設計 → setup 問題 |
| S4 | 45% | B2-M4 再現 (2-turn 体験が継続) |
| S5 | 30% | B2-M1 再現 (hallucination 継続) または partial improvement |

合計予想: HIGH 0-1 件 (S1 が想定外経路で新規 HIGH を出す可能性)、
MED 2-3 件 (S4/S5 open 確認)、 LOW 数件。

---

## バッチ完了基準

- 5 scenario 全実行完了
- 各 scenario について 6 観点 + WAL/events grep 観測を記録
- 各 scenario について `:cost` の値を記録
- findings.md + per-finding split (必要に応じて)
- A4 で user レビュー後、 process 継続可否を確認

---

## A2 review request

- S3 (nested skill) の setup: `eval_builder` が run_skill を使う設計か事前確認が必要か?
  skill.md を read してから実行するか、 それとも起動して観測する形でよいか?
- S2 (ask_user) で `report.md` 不在の誘発が弱ければ、 `--no-restore` 後に
  `/tmp/nonexistent.md` 等の絶対パスで曖昧さを除いた方がよいか?
- 5 件の順序、 変更要望あれば。
