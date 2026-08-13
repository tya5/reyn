---
type: concept
topic: codeact
audience: [human, agent]
---

# CodeAct: Code-as-Tools（コードとしてのツール）

CodeAct は Reyn の [tool-use scheme](tool-use-schemes.md) の 1 つ — LLM に
tool をどう見せ、 LLM の呼び出しをどう dispatch するか、 の方式である。 default
scheme が tool を JSON function definition として提示し LLM が JSON tool-call を
出すのに対し、 **CodeAct は tool を code API として提示し、 LLM はそれを呼ぶ
Python snippet を書く**。 モデルの仕事が「tool call を出す」から「コードを書く」
へ移る。

このページは deep dive。 全 scheme の一覧と選び方は
[Tool-Use Schemes](tool-use-schemes.md) を参照。

## 何か (What)

CodeAct では LLM は構造化された tool-call を返さない。 メッセージ内に fenced な
Python ブロックを書き、 tool 使用はその **内部** の通常の関数呼び出しとして起きる:

```python
result = tool('read_file', path='README.md')
```

各 `tool(...)` 呼び出しは 1 つの action を実行し結果を返す（action が denied /
excluded / unknown なら raise）。 モデルはコードで作業を組み立て — loop・中間
変数・条件分岐 — 外界には `tool()` を通じて **のみ** 到達する。

## どう動くか (How it works)

### code API

JSON `tools=` payload の代わりに、 CodeAct は **code API** を作る: モデルが
呼べる action の reference list を、 system prompt に `tool(...)` signature として
render する。 permission-eligible な各 action が 1 行 — 名前・引数・短い説明 — で
並ぶ。 この list は *提示のみ*。 モデルはそれを読んで何が callable かを知る。
CodeAct には JSON tool channel が **そもそも存在しない** — 「channel はあるが中身が
空」とは別のことであり、何も advertise されていないのに code API からは全 action が
呼べる理由でもある。

**何が載るか**: `enumerate-all` scheme が `tool_calls` で advertise するのと同じ
集合 — base tool（`read_file` / `call_mcp_tool` / `spawn_session` …）と
[universal catalog](universal-catalog.md) の全 action（`glob_files` /
`run_pipeline` …）。

base tool が既に名前を持つ capability は、 非修飾名で **1 回だけ** 載る。
catalog の `call_mcp_tool` と base の `call_mcp_tool` は同じ操作
なので、 code API に書かれるのは `call_mcp_tool` だけ。 修飾名の方も
**呼べる** ままである — dispatchable set には残っているので snippet 内からの
`tool('call_mcp_tool', …)` は通常どおり応答される — 単に 2 度目の
advertise をしないだけ。

### sandboxed subprocess

snippet は agent のプロセス内では走らない。 platform sandbox backend（macOS は
Seatbelt、 Linux は Landlock）の下、 **sandboxed subprocess** で実行される。
snippet 内からの直接の filesystem write・network access・subprocess spawn は
sandbox にブロックされる — snippet が外界へ到達する唯一の正規チャネルは `tool()`。

### duplex permission-proxy socket

ここが load-bearing な部分。 snippet は sandbox 内で走り、 自身は permission
権限を **持たない**。 では snippet 内の `tool()` 呼び出しはどう action を実行するのか？

各 `tool(...)` 呼び出しは名前と引数を `AF_UNIX` socketpair 経由で **parent**（agent
プロセス）へ marshal し、 parent が request を処理して結果を書き戻す。 parent
側では、 その request は **他のどの scheme の tool call も通るのと同一の gate** を通る:

```
tool('x', ...)  →  socket  →  parent: exclude-check → permission-check → dispatch_tool  →  result  →  socket  →  snippet
```

子の snippet は Reyn 内部に一切触れない。 OS が eligible にしていない tool には
到達できず、 すべての呼び出しは副作用の前に permission-check される。 ゆえに
CodeAct 呼び出しは等価な JSON 呼び出しと **少なくとも同等に厳格** に gate される
— 同一 gate に *加えて* sandbox containment。

これが鍵となる性質: **scheme は LLM への提示面 — tool 使用の表現方法 — だけを
変え、 何を許可するかは変えない。** exclude → permission → dispatch pipeline
(P4/P5) は不変。 CodeAct に切り替えても security も validation も弱まらない。

## turn contract（ターン契約）

CodeAct の 1 ターンは次の **どちらか一方のみ**、 両方は不可:

1. **action turn** — 単一の fenced ` ```python ` ブロックのみ（前後に prose なし）。
   モデルはそのブロックで action を実行する。
2. **final answer** — code ブロックなしの plain prose。 これがターンを終える。

action ブロック内で、 モデルは計算が終わったら最終回答を `result` に代入する。
`tool(...)` が action を取る唯一の手段。 モデルが答えを得てこれ以上 action が
不要になったら、 code ブロックなしの plain prose で返す — それがターン完了の
signal。 fenced ブロックのない応答は plain-prose final answer として読まれ、
**実行すべき bare code ではない**。

## いつ使うか (When to use)

CodeAct は opt-in。 選ぶ価値があるのは:

- **モデルが JSON tool-call よりコードを確実に扱う時。** 一部のモデル — 特に
  弱めのモデル — では、 tool 使用をコードで表現する方が整形式 JSON tool-call を
  出すより確実で、 tool-use の信頼性が上がる。
- **タスクが 1 ターンで多段を合成する時。** `list → loop-read → aggregate` を
  短いプログラムとして書けば、 N 回の逐次 round-trip tool-call が要る処理を
  1 ターンで行える。

それ以外では default の `universal-category` scheme を使う。 CodeAct の価値は
上記 2 状況に特化している。

## 有効化 (How to enable)

CodeAct は `reyn.yaml` の `tool_use.scheme` x `tool_use.transport` の 2-key
config で chat レイヤーに選ぶ（FP-0066 P4, #3247）。CodeAct は
`enumerate-all` presentation を `content_fence` transport 上で表現したもので、
独立した scheme 名を持たない。 chat レイヤーの default は
`scheme=enumerate-all` / `transport=tool_calls` ゆえ CodeAct は opt-in:

```yaml
# reyn.yaml
tool_use:
  scheme: enumerate-all
  transport: content_fence   # top-level chat router
```

**カタログが大きい場合**: `scheme: category` + `transport: content_fence` は、
同じ code-as-tools transport を `universal-category` のサーフェス上で表現した
ものです。モデルはやはりフェンス付き Python を書きますが、見せられる関数は
全アクションではなくカタログのラッパー（`invoke_action` など）なので、システム
プロンプトがカタログと共に増えません。CodeAct を選ぶ理由は当てはまるが全件列挙
のコストが高すぎる、という場合に使います。

**カタログが非常に大きく、ブラウズが入口として適切でない場合**: `scheme:
retrieval` + `transport: content_fence` は `list_actions` の代わりに
`search_actions` を見せます。モデルは欲しいものを記述し、検索が返したものを
その場で呼び出せます — `tool_calls` 側の retrieval セルが払う再提示の 1 往復
なしに、同一スニペット内で完結します。`embedding.enabled: true` が必要です
（加えて `embedding.index.actions`、既定 **on** — 通常のケースでは追加設定不要。
[`embedding.index`](../../reference/config/reyn-yaml.md#embedding-fields) 参照）。

旧 `tool_use.chat` key は #3247 で削除済み（clean-break、compat alias 無し）
— これを書いた `reyn.yaml` は parse 時に失敗する。
[`reyn.yaml` § tool_use](../../reference/config/reyn-yaml.ja.md#tool_use-block) 参照。

## security note（セキュリティ注記）

load-bearing な保証を再掲: **どの scheme が active でも、 全 tool 呼び出しは
同一 gate を通る** — eligibility (exclude) → permission → dispatch。 CodeAct は
sandboxed subprocess を加え、 snippet 内の各 `tool()` 呼び出しを
permission-proxy socket 経由で同じ parent 側 gate に通す。 snippet はそれを
迂回できない。 CodeAct を選ぶことはモデルの tool 表現方法を変えるだけで、
モデルに許可される内容は変えず、 Reyn の permission / validation model を
弱めない。 [Permission model](../runtime/permission-model.md) 参照。

## 関連 (See also)

- [Tool-Use Schemes](tool-use-schemes.md) — scheme 一覧 + 選び方
- [Universal Action Catalog](universal-catalog.md) — default scheme の内部
- [Permission model](../runtime/permission-model.md) — 全 scheme が dispatch する gate
