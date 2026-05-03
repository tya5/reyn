# Batch 1 (Practice) — Scenarios

> 練習 batch。 process が成立するか確認するための 3 件。 OK なら本 batch で
> 件数を増やす予定でした。

## 共通前提

- LLM: LiteLLM proxy at `localhost:4000`、 model `openai/gemini-2.5-flash-lite`
- 実行 dir: `~/Workspace/junk/claude_sandbox/sandbox_2`
- TUI mode (default) と CUI mode の両方で気になれば確認、 通常は CUI で記録しやすい
- 各 scenario 開始時に `--reset` で clean state 推奨 (実際は `--no-restore` を使用 — `--reset` の confirmation prompt が piped stdin で扱いにくい)
- 本来 simple_memo_app を使う予定だったが、 `reyn skills` 一覧に存在しなかったので scenario 1 を `text_summarizer` に変更 (= `dsl/local/` ではなく `reyn/local/` が search path だった)

## 観測の枠組み

各 scenario について以下を記録:

| 観点 | 何を見るか |
|---|---|
| **応答品質** | LLM の文章が用件に合ってるか、 冗長 / 簡潔過ぎないか、 日本語として自然か |
| **意図解釈** | skill router / agent の routing が user 意図に沿うか、 想定外 skill が起動しないか |
| **待ち時間** | 1 turn の応答までの時間 (体感秒数)。 「会話のテンポ」 が成立するか |
| **見せ方** | 内部状態 (phase / control_ir / chain / agent name) の露出が適切か |
| **エラー UX** | 失敗時の文言が user にとって actionable か、 救出方法が分かるか |
| **state 整合性** | events log / WAL / snapshot が想定通りに進むか (技術観測、 user 視点の二重チェック) |

---

## Scenario 1: 基本 user flow — テキスト要約

### 目的

skill router が user 意図を skill にマップし、 skill が走り、 narrator が
完了を報告する **最も基本的な 1 ターン体験**。

### Action

`reyn chat default --cui --no-restore` を起動、 1 ターン:

```
"次の英文を 3 つの bullet point に要約して: Python is a high-level programming
language created in 1991 by Guido van Rossum. ..."
```

### 期待結果

- skill router が `text_summarizer` の対応 phase を invoke
- skill が要約処理
- narrator が完了応答を user に返す

### 観測ポイント

- 「要約して」 で本当に text_summarizer が起動するか、 もしくは別 skill
  (例: skill_builder の誤起動) が出るか
- 完了報告は何と言ってきたか、 user 入力の値が報告に含まれるか
- skill router classify + skill 実行 + narrator の合計時間
- skill が見つからない場合の応答 (= 素直に「なし」 か、 hallucinate か)

---

## Scenario 2: Multi-agent delegate

### 目的

main agent から specialist agent に delegate、 specialist が応答、
chain 経由で main に reply 戻り、 user が結果を受け取る。 multi-agent
構成が user 視点でどう見えるかを観測。

### Setup

- 2 agent: `default` (main role) と `specialist` (専門 role)
- `_default` topology (PR13)
- `specialist` を `reyn agent new specialist` で作成、 profile に role 記述
  (「料理レシピの専門家。 簡単で実用的な作り方を答える。」)

### Action

```
$ reyn chat default --cui --no-restore
> specialist エージェントに「カレーの簡単な作り方」を聞いて教えて
```

### 期待結果

- default agent が `delegate_to_agent` tool を使って specialist に delegate
- chain_id が割り振られ、 default agent は「peer reply 待ち」 状態に
- specialist agent が起動、 「カレーの簡単な作り方」 を返す
- chain 経由で reply が default agent に届き、 default agent が user に
  最終応答

### 観測ポイント

- user は「default が specialist に聞いて、 specialist が答えて、 default
  が転送した」 流れを認識できるか
- TUI / CUI で agent name が分かりやすく出るか (`[specialist]:` 的な prefix)
- 2 LLM 呼び出し分の体感時間
- もし途中で詰まったら user は何が起きてるか分かるか

---

## Scenario 3: 新機能 — skill-only permission gating の startup_guard

### 目的

最近 land した「skill-only permissions」 (commits 246ce42 + 7b9adc1 +
950592e) で、 stdlib skill `read_local_files` が startup_guard を発火、
user が permission 確認に答える UX を観測。

### Setup

- `read_local_files` stdlib skill (= `permissions: mcp: [filesystem]` を
  skill-level で declare)
- LiteLLM proxy + filesystem MCP server が立っている前提 (memory:
  `project_local_env.md`) — ※ 実は MCP は未設定だった、 後述
- `--no-restore` で clean state

### Action

```
$ reyn chat default --cui --no-restore
> read_local_files skill で /path/to/README.md を読んで要約して
```

### 期待結果

- skill router が `read_local_files` を選択
- skill 起動前に startup_guard が `mcp: [filesystem]` の承認を user に prompt
- user が承認 ([y]es / [j]ust this path / [r]ecursive / [N]o)
- 承認後 skill 進行、 file 読み取り → 要約 → narrator 応答

### 観測ポイント

- permission 確認の文言が分かりやすいか
- skill 起動の前に prompt が出るか
- もし phase 単位 permissions: が残ってる skill だったら hard reject される
  — その文言が「skill.md frontmatter に declare」 を指示しているか
  (= ADR-0020 のメッセージ)
- 承認した permission が `.reyn/approvals.yaml` に persist されるか

---

## バッチ完了基準

- 3 scenario 全実行完了
- 各 scenario について 6 観点で 1 行 finding 以上記録
- 私の所感を 1 scenario あたり 2-5 行
- A4 で user レビュー後、 process 自体に問題が無いか確認

## 改善の予感 (バッチ前推測、 A4 で answer 合わせ)

私が test 越しで気になっている候補:

- skill router の意図解釈は LLM 次第で揺れやすい (gemini-2.5-flash-lite
  の精度依存)
- narrator の応答品質は phase 出力 + skill description だけで作るので、
  user の入力値が response に含まれない可能性
- multi-agent delegate の chain 経路は internal にしては user に滲んでいる
  かも (= 「awaiting peer reply」 等の internal 用語)
- startup_guard の prompt 文言は技術寄り (= path / scope / [j] 等)、
  非技術 user には難しい

これらが当たるか / 外れるかを dogfood で確認、 それ以外の予期しない
finding が出れば valuable。

---

## ※ Spoiler

[findings.md](findings.md) を読むと分かるが、 **私の事前仮説はほぼ全て
外れた**。 「skill router の意図解釈は LLM 次第」 ではなく「skill
router 自体が起動しない」 という根本的な問題で、 multi-agent delegate も
「user 視点で滲む」 どころか「動かない」 という結末でした。

事前 prediction の精度: 当たり 0/4。 dogfood で得られた finding: 11 件。
