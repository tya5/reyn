# Batch 2 (Real) — Scenarios

> 練習 batch (= batch 1) で 11 件発覚した HIGH bug が修正された前提で、
> regression 確認 + 新領域開拓を行う本格 batch。 5 scenario。

## 共通前提

- LLM: LiteLLM proxy at `localhost:4000`、 model `openai/gemini-2.5-flash-lite`
- 実行 dir: `~/Workspace/junk/claude_sandbox/sandbox_2`
- 通常は CUI mode で記録 (`--cui --no-restore`)
- batch 1 から変わった主要 commit: `e59cead` / `9e8126c` / `651d7f3`
  (= prelude.md 参照)

## 観測の枠組み (batch 1 と同じ 6 軸)

| 観点 | 何を見るか |
|---|---|
| **応答品質** | LLM の文章が用件に合ってるか、 冗長 / 簡潔過ぎないか、 自然か |
| **意図解釈** | skill router / agent の routing が user 意図に沿うか、 想定外 skill が起動しないか |
| **待ち時間** | 1 turn の応答までの時間 (体感秒数)。 「会話のテンポ」 が成立するか |
| **見せ方** | 内部状態 (phase / control_ir / chain / agent name) の露出が適切か |
| **エラー UX** | 失敗時の文言が user にとって actionable か、 救出方法が分かるか |
| **state 整合性** | events log / WAL / snapshot が想定通りに進むか (技術観測) |

## 構成

| ID | 種別 | カバー領域 | 期待 |
|---|---|---|---|
| S1 | regression | F3 (router 起動: text 要約) | text_summarizer 起動 |
| S2 | regression | F9 + F10 (skill 名明示 + MCP setup) | read_local_files 起動 + MCP 動作 |
| S3 | regression | F5+F6+F7+F8 (multi-agent delegate 連鎖) | 「16 秒の悲劇」 cascade 再発無し |
| S4 | new | skill + ask_user (= startup_guard / permission prompt 経路) | LLM が ask_user op を投げ、 user 回答後 skill 継続 |
| S5 | new | memory remember + recall | `remember_shared` で書き、 次 turn で recall |

> **defer (batch 3 へ)**: nested skill (= run_skill IR op、 parent → child)、
> chat compaction 境界 (= 30+ turn での head/body/tail)、 Q2 output_language
> unset の e2e (= Tier 2 で pin 済みなので e2e 不要と判断)。
>
> いずれも触りたい領域だが時間 / LLM cost 大、 もしくは Tier 2 test 等価で
> 代替可能。 batch 2 で HIGH bug 再発無しと確認できれば batch 3 で扱う。

---

## Scenario 1 (regression): text 要約 — router 起動

### 目的

F3 修正後、 「要約して」 で `text_summarizer` skill が **本当に invoke されるか**
を実 LLM で確認。 batch 1 では skill が呼ばれず LLM が直答した。

### Setup

`reyn chat default --cui --no-restore`、 1 ターン。

### Action

```
"次の英文を 3 つの bullet point に要約して: Python is a high-level programming
language created in 1991 by Guido van Rossum. It emphasises code readability
and supports multiple programming paradigms."
```

### 期待結果

- `events skill_runs/2026-05-04*` に text_summarizer の entry が出る
- WAL に `skill_dispatch` event が出る
- agent reply は narrator 経由の完了報告 (skill 出力 + 自然言語 wrap)

### 観測ポイント (6 軸)

- **意図解釈**: 確実に text_summarizer が起動するか、 別 skill 誤起動 / 直答に
  逃げないか
- **応答品質**: 要約の品質、 narrator の文章
- **待ち時間**: skill 起動 + LLM 経由 phase 実行 + narrator の合計
- **state 整合性**: skill_runs entry 存在、 phase artifact が workspace に出る
- **エラー UX / 見せ方**: skill 経由の応答が batch 1 の直答 path より
  「機械的」 にならないか (= narrator の人間味)

---

## Scenario 2 (regression): 明示 skill 名 + MCP — F9+F10 retest

### 目的

F9+F10 修正後の確認。 read_local_files skill を **explicit に skill 名指定** で
呼び、 MCP filesystem server を **事前設定** した上で permission prompt UX を
観測。

### Setup

事前準備 (前回 batch 1 で抜けていた):

1. `examples/configs/with-mcp.yaml` を base に reyn.local.yaml を更新
   (filesystem MCP server を追加)
2. もしくは `reyn init` 後の commented MCP block を un-comment して使う

`reyn chat default --cui --no-restore`、 1 ターン。

### Action

```
"read_local_files skill を使って /Users/yasudatetsuya/Workspace/junk/claude_sandbox/sandbox_2/README.md を読んで、 何の project か 1 段落で説明して"
```

### 期待結果

- skill_router が read_local_files を invoke (= F9 修正の効果)
- MCP filesystem server で file 読取り (= F10 修正の効果)
- 読取りに permission が要れば user に prompt
- 完了後 narrator が summary 応答

### 観測ポイント (6 軸)

- **意図解釈**: explicit skill name で確実に router が起動するか
- **エラー UX**: MCP 設定漏れだった場合のメッセージは actionable か
  (例: 「filesystem MCP server is not configured. Add to reyn.yaml: ...」)
- **見せ方**: permission prompt が出るとしたら表示は分かりやすいか
- **state 整合性**: MCP call 後の events と WAL

---

## Scenario 3 (regression): multi-agent delegate — 「16 秒の悲劇」 retest

### 目的

F5+F6+F7+F8 修正後の確認。 batch 1 の「16 秒の悲劇」 cascade が再発しないか、
そして specialist の本物のレシピが今度は **default 経由で user に届く** か。

### Setup

事前準備:

- `reyn topology` で `_default` topology に `specialist` agent が居ることを確認
- specialist の profile.yaml に料理系 expertise を declare (既にあれば skip)
- `reyn chat default --cui --no-restore`

### Action

```
"specialist エージェントに「カレーの簡単な作り方」 を聞いて教えて"
```

### 期待結果

- delegate_to_agent が **1 回だけ** dispatch (= F5: dedupe で 2 重 inbox_put 防止)
- specialist が LLM の最終応答が出るまで agent_response を **送らない**
  (= F6: 早期空 reply 防止)
- default 側、 retry を起こさず 1 度の specialist reply で完了
  (= F7: 空 reply 誤解 cascade 防止)
- 最終的に user に **完全なカレーレシピ** が届く (= F8 fallback message を
  踏まない)
- 16 秒以内ぐらいの reasonable 待ち時間

### 観測ポイント (6 軸)

- **意図解釈**: default → specialist routing 起動率
- **state 整合性**: WAL の inbox_put 数 (= 1 件であるべき)、
  agent_response の数 (= 1 件であるべき、 空 reply 無し)、
  events log の `tool_call_deduped` (= 偶発的に出るかもしれない、 出てても
  問題なし)
- **応答品質**: レシピが届くか、 内容も妥当か
- **待ち時間**: cascade 無しなので 5-10 秒程度を期待
- **エラー UX**: もし失敗したらどう失敗するか (= "[specialist: could not
  produce a reply ...]" marker が default 経由で user へ届くか)
- **見せ方**: chain delegation の進行が user に滲むか (= internal だが
  完全 silent でもなさそう)

---

## Scenario 4 (new): skill + ask_user — startup_guard 経路

### 目的

skill 実行中に LLM が `ask_user` IR op を発行 → user に prompt → user 回答 →
skill 継続、 という UX を観測。 batch 1 では skill が起動しないので touch
できなかった経路。

### Setup

ask_user を発行する skill を選ぶ。 候補:
- `read_local_files` の path 確認 prompt (permission gating の一部)
- `eval` skill の case 数確認
- 自作 skill (簡単な質問 skill を `reyn/local/` に作って試すのも可)

最も自然なのは `read_local_files` で **存在しない / 曖昧な** path を要求し、
LLM に「どの path?」 と ask_user させる。

### Action

```
"先週書いた company の Q3 report を読んで要約して"
(= 具体 path 不明、 LLM は ask_user で「どの path?」 と聞くハズ)
```

### 期待結果

1. skill_router が read_local_files (or 関連 skill) を invoke
2. skill phase が ask_user IR op を発行
3. CUI に「どの path ですか?」 のような question が出る
4. user が `~/Documents/Q3-report.md` 等の path を回答
5. skill が file 読取り → 要約 → 完了

### 観測ポイント (6 軸)

- **見せ方**: ask_user prompt の表示が CUI で読めるか、 言語 (= user input
  に追従、 Q2 のおかげで強制 ja は無いハズ)
- **state 整合性**: ask_user → answer の cycle が events に正しく記録、
  WAL で intervention dispatch / resolved event
- **エラー UX**: user が回答せず Ctrl-C すると?  (= ask_user の cancel UX、
  別 scenario でも touch するが軽く)
- **応答品質**: user の回答後、 skill が文脈を保って続くか (= compaction
  境界跨ぎ無いか)

---

## Scenario 5 (new): memory remember + recall

### 目的

`remember_shared` / `remember_agent` tool で memory に書き、 次 turn で同 chat
session 内で recall できるか観測。 batch 1 では skill 一切起動せず memory
にもアクセスしなかったので未確認領域。

### Setup

`reyn chat default --cui --no-restore`、 2 ターン以上。

### Action

ターン 1:
```
"私は Python が好きで、 Reyn project を試している tetsuya です。
これを覚えておいて。"
```

ターン 2:
```
"私について何か知ってることある？"
```

### 期待結果

ターン 1:
- LLM が `remember_shared` tool を call (= name "user" 系の memory)
- shared memory に file が出る (`<project>/.reyn/memory/shared/user_*.md`)
- 完了応答「覚えました」 等

ターン 2:
- LLM が memory index を見て該当 entry を発見
- `read_memory_body` で内容を読み (= description で十分なら index だけ)
- 「Python が好きな tetsuya さんですね」 のような recall reply

### 観測ポイント (6 軸)

- **意図解釈**: 「覚えて」 で remember tool 起動、 「私について」 で
  recall path
- **state 整合性**: memory file が存在 / 形式 / front-matter 正しい
- **見せ方**: tool call の発生が user に滲むか (CUI でどう表示)
- **応答品質**: recall reply が memory entry を反映、 fabrication しない

---

## 事前 prediction (assistant)

batch 1 で当たり 0/4 だった反省を踏まえて、 強気に書かない:

- **S1**: 起動するハズ。 だが弱モデルの attractor 次第で 50% 確率で再発も
- **S2**: MCP 設定が正しければ動くハズ。 私が config 書き間違えるリスク
  > bug リスク
- **S3**: F5 dedupe は決定論的に動く。 F6 marker も。 F7 cascade は理論上
  消える。 95% 確率で pass
- **S4**: ask_user 経路、 LLM が ask_user IR op をいつ出すかが弱モデル
  依存。 30% で「不要なくらい」 直接答える可能性
- **S5**: memory tool は PR15 で landed、 batch 1 では未経由。 60% で動く

合計予想: HIGH 0-1 件、 MED 2-3 件、 LOW 数件。

(前回も予想と現実が逆方向に外れたので、 これも参考程度に)

---

## A2 review request

user へ:

- S2 (MCP setup) は事前設定必要、 batch 2 開始前に setup commit を別途
  入れる? もしくは scenario 内で対処
- S4 (ask_user) の prompt は曖昧 path で trigger するつもりだが、 もっと
  自然な ask_user trigger ある?
- 5 件の順序、 後ろ送り / 前倒しで好みは?

A2 review 後 v2 → A3 実行。
