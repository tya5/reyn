# Batch 1 (Practice) — Findings

> 練習 batch のつもりが、 起動段階で躓き、 そこから先も**全 scenario で
> skill_router が起動しない** という結果になった事件記録。

このファイルは index です。 各 finding の詳細は `findings/` 配下に分割して
保存しています (= 1 finding を読みたいときに 1 ファイルだけ load 可能 =
読み出しコスト削減)。

## 概要

> 「で、 何が起きたの?」 の一行サマリ。 詳細は各 link 先へ。

| ID | 重要度 | 一行で言うと | 状態 |
|---|---|---|---|
| [F1](findings/F01-chat-startup-attribute-error.md) | HIGH | reyn chat を起動した瞬間 `AttributeError`。 「Did you mean」 まで親切な Python だが起動はしない | **fixed** at `f5b3281` |
| [F2](findings/F02-reyn-local-yaml-docstring-mismatch.md) | LOW | `reyn.local.yaml` は実際 load されているのに、 `config.py` の docstring には「そんな file 知らん」 と書いてある | deferred (Wave B) |
| [F3](findings/F03-skill-router-direct-reply.md) | HIGH | 「要約して」 とお願いしたのに `text_summarizer` skill は呼ばれず、 LLM が「自分でやれます」 と直答 | **fixed** at `e59cead` |
| [F4](findings/F04-cost-always-zero.md) | LOW | LLM 応答は来てるのに `cost -- prompt=0 completion=0 total=0`。 永遠の 0 円 | deferred (Wave B) |
| [F5](findings/F05-delegate-double-inbox-put.md) | HIGH | LLM が `delegate_to_agent` を 1 回呼んだのに、 specialist の inbox には同じ依頼が **2 件** 届く | **fixed** at `9e8126c` |
| [F6](findings/F06-specialist-empty-early-reply.md) | HIGH | specialist 側、 LLM がまだ考え中なのに「答えました (中身: 空)」 を default に送りつける | **fixed** at `9e8126c` |
| [F7](findings/F07-default-misreads-empty-reply.md) | MED | default、 specialist の空 reply を「失敗」 と判定して再 delegate。 retry budget 即枯渇 | **fixed** at `9e8126c` |
| [F8](findings/F08-retry-exhausted-english-fallback.md) | MED | 諦めるときに出るエラー文が英語。 user は日本語で話してた。 内容も「rephrase してね」 で誤誘導 | **fixed** at `e59cead` |
| [F9](findings/F09-explicit-skill-name-ignored.md) | HIGH | `read_local_files skill で〜` と skill 名を本文に直書きしても router は無視。 routing 0/3 | **fixed** at `e59cead` |
| [F10](findings/F10-filesystem-mcp-not-configured.md) | HIGH | `read_local_files` は filesystem MCP を要求するが、 そんな MCP server はどこにも設定されていない | **fixed** at `e59cead` |
| [F11](findings/F11-router-japanese-fallback-english.md) | MED | router の fallback / clarifying path だけ英語固定。 ja 設定しても抜けてくる | **fixed** at `e59cead` |

**skill_router の起動成功率: 0/3。** これが batch 1 の headline。

**A5 完了状況** (2026-05-04 commit `e59cead` + `9e8126c`):
HIGH 6 件 + MED 2 件 = **8 件 fixed**、 LOW 2 件 (F2 / F4) は **Wave B
coverage audit に deferred**。 Batch 2 は HIGH bug fix 後に予定通り実施。

> 注: 上記の **fixed** 表記は OS-level Tier 2 invariant で pin した状態。
> e2e 実 LLM での再現確認 (= regression dogfood) は **batch 2 で実施予定**。

---

## ハイライト narrative

batch 1 の事件記録 — 各 finding を読み解いていく順序付け。

### Round 0 — 起動すら拒否される (F1)

scenario 1 を試そうとして、 `reyn chat default --cui --no-restore` を打った
瞬間、 chat が起動しない。 `_active_interventions` が無いと Python が叫ぶ。
PR-refactor-session-1 の wave 1C でリネームした attribute の漏れだった。

詳細: [F1](findings/F01-chat-startup-attribute-error.md)

修正後にようやく scenario が走り出す。

### Round 1 — skill_router 起動 0/3 (F3 + F9)

3 scenario すべてで skill_router が起動しない。 「要約して」 (implicit
intent) でも、 「read_local_files skill で〜」 (explicit skill name) でも、
LLM は skill を選ばず直接答える。 PR35 の native tool_use loop が、 弱モデル
(gemini-2.5-flash-lite) では「面倒くさい skill 呼ばずに直答する」 attractor
にハマる。

詳細: [F3](findings/F03-skill-router-direct-reply.md) /
[F9](findings/F09-explicit-skill-name-ignored.md)

これが「現状人間視点だと chat の会話は使い物にならない」 (user 評、
2026-05-04) の正体の半分。

### Round 2 — F5-F8 「16 秒の悲劇」 multi-agent delegate 連鎖事故

scenario 2 (`specialist エージェントに「カレーの簡単な作り方」 を聞いて`)
で発生した複合 bug。 単独でも HIGH だが、 4 件が連鎖して final UX が
破綻する dogfood 史に残る (?) cascade。

```
08:26:23  user message receive
08:26:24  default agent の LLM、 delegate_to_agent を 1 回呼ぶ
          → だが WAL には inbox_put が **2 件** 書き込まれる   ← F5
08:26:25  specialist agent が 1 回目の request を consume、 LLM 起動
08:26:30  specialist が describe_skill tool を call
08:26:34  specialist の router_loop、 まだ skill 実行前なのに
          response: "" の agent_response を default に送る   ← F6
08:26:34  default 受領、 「peer 失敗」 と判断、 delegate を再試行   ← F7
08:26:39  specialist の router_loop、 また response: "" 送る
08:26:39  default、 また再試行 (retry 2/3)
08:26:40  default 側 retry budget 枯渇、 英語 fallback を表示   ← F8
08:26:42  specialist の LLM、 ようやくレシピ確定 (実は良質)
          → だが default 側 budget 切れで discarded
```

specialist は実は **完全に正しいカレーレシピを生成していた**。 ただ届く
頃には default 側で「もう諦めた」 状態だった。 user に届いたのは:

```
[error] Router exhausted retry budget (3/3) for this turn. Last reason: (none).
        Falling back to direct reply.
agent> I couldn't find a way to handle that within this turn's routing budget.
       Please try rephrasing or breaking the request into smaller pieces.
```

英語、 non-actionable、 何も解決しない。 specialist の労作はどこにも届かない。
これが「使い物にならない」 と user が言いたくなる体験そのもの。

詳細:
[F5](findings/F05-delegate-double-inbox-put.md) /
[F6](findings/F06-specialist-empty-early-reply.md) /
[F7](findings/F07-default-misreads-empty-reply.md) /
[F8](findings/F08-retry-exhausted-english-fallback.md)

### Round 3 — out-of-box experience の崩壊 (F10 + F11)

scenario 3 では skill が要求する filesystem MCP server が **どこにも
設定されていない** ことが判明 (F10)。 加えて router の fallback / clarifying
path が日本語 user に英語で応答を返す (F11)。

詳細: [F10](findings/F10-filesystem-mcp-not-configured.md) /
[F11](findings/F11-router-japanese-fallback-english.md)

### Round 4 — 計測の盲点 (F2 + F4)

dogfood の主観点と独立だが、 LOW 2 件:

- F2: `reyn.local.yaml` が動くのに docstring に書いてない
- F4: cost が永遠の 0 (LiteLLM proxy 経由で usage が落ちる)

詳細: [F2](findings/F02-reyn-local-yaml-docstring-mismatch.md) /
[F4](findings/F04-cost-always-zero.md)

両方 Wave B coverage audit でまとめて整理。

---

## まとめ — 練習 batch のはずが

3 scenario、 11 finding (重複除外で 10)。 練習バッチとしては多すぎる。

**全 scenario 共通の最重要 finding**:

1. **F1**: chat が起動しない (即修正済)
2. **F3 + F9**: skill_router が誰の言うことも聞かない (3/3 で起動失敗)
3. **F5 + F6 + F7 + F8**: multi-agent delegate が連鎖 bug で完全停止

これらは「現状人間視点だと chat の会話は使い物にならない」 の中身を構成
する根本問題と言ってよさそう。 dogfood で見えたものは大きい。

### バッチサイズの教訓

「練習として 2-3 scenario」 という user の指示は正しかった。 もし最初から
8-10 scenario 流していたら、 F1 の段階で全 scenario が die して何も
発見できなかった。 小バッチで先に process 検証する戦略が正解だった。

### 私の事前 prediction の精度

scenarios.md 末尾に書いた事前仮説 4 件:

- skill router の意図解釈は LLM 次第で揺れやすい — **外れ** (= router が
  そもそも起動しない)
- narrator の応答品質は phase 出力 + skill description だけで作る — **検証
  不能** (= skill 経由しないので narrator が呼ばれない)
- multi-agent delegate の chain 経路は internal にしては user に滲んでいる
  — **外れ** (= 滲む以前に動かない)
- startup_guard の prompt 文言は技術寄り — **検証不能** (= startup_guard
  までたどり着かない)

精度: 当たり 0/4。 ただし「現実は私の予想以上に深刻」 という方向への
外し方なので、 dogfood の意義は十分。

### 次のアクション

A5 wave (HIGH bug fix 4 PR) は **完了** (commit `e59cead` + `9e8126c`)。
残り工程:

- **batch 2 dogfood** で各 finding の e2e 改善確認 (= regression net)
  + batch 1 で抜けた領域 (skill 起動 + ask_user / nested skill /
  postprocessor / chat compaction / memory 操作)
- **Wave B coverage audit** で F2 + F4 を含む docs / config / cost 系
  の test gap 整理
- **Wave C OSS Lv.1**: CI / README rewrite / CONTRIBUTING.md
