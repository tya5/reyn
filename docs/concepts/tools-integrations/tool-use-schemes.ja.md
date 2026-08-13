---
type: concept
topic: tool-use-schemes
audience: [human, agent]
---

# ツール使用スキーム

agent のツールを LLM にどのように見せるか、そして LLM の呼び出しをディスパッチされたアクションに戻す方法は、**プラガブルなスキーム**です。Reyn は 4 種類を同梱しており、`reyn.yaml` で chat レイヤーに 1 つを選択します。デフォルトは `enumerate-all` です。chat レイヤーは別のスキームに切り替えられます。

重要な不変条件：**スキームは LLM 向けのサーフェスのみを変更します**。どのスキームが生成したツール呼び出しであっても、同一の OS ゲート（除外チェック → パーミッションチェック → ディスパッチ）を通ります。スキームを変えても許可される内容は変わらず、LLM への表現方法だけが変わります。詳細は[パーミッションモデル](../runtime/permission-model.md)を参照してください。

## 4 つのスキーム

横断的な知見（H1）：**ツール名の可視性が呼び出し成功率を左右します**。呼び出し可能な名前を LLM 向けサーフェスに直接置くスキーム（`enumerate-all`、`CodeAct`）はモデルが推測なしに呼び出せます。名前を迂回路の後に置くスキーム（`universal-category` の discover→invoke、`retrieval` の search-first）はツールで名前幻覚を誘発します。これが chat デフォルトを `enumerate-all` に変更した理由です。

### `enumerate-all`（chat デフォルト）

使用できるすべてのツールをフラットに LLM のツールリストに提示し、名前でディスパッチします。発見の迂回路のないシンプルなネイティブ JSON ベースラインです。**`chat` レイヤーのデフォルト**：アクションをフラットに列挙することで LLM が直接呼び出せるようになり、discover-then-call の迂回路が誘発していた `invoke_action` の名前幻覚が解消されます（chat パスで direct ツール使用が ~30%→100% に改善）。`tool_use.scheme` を未設定のままにするとこのスキームが使われます(FP-0066 P4b #3247)。

**使いどころ：** chat のデフォルト。直接的で決定論的な名前→ディスパッチ。トレードオフは**可視性コスト**であり、弱いモデルへのペナルティではありません：カタログが大きくなると（H1 では ~67 ツール ≈ ~50KB のツールサーフェス、`universal-category` の ~3.2 倍）リクエストサイズが線形増加します。この可視性こそが弱いモデルのツール使用を改善する要因であり、コストはトークンであって、非常に大きなカタログでのみ問題になります（`universal-category` を参照）。

### `universal-category`

[ユニバーサルアクションカタログ](universal-catalog.md)：スキル、MCP ツール、メモリエントリ、ファイル op、インデックス済みコーパスがすべて単一の修飾名でアドレス指定でき、少数の固定ラッパー（discover → describe → invoke）経由で到達します。LLM 向けのツールリストはカタログが成長しても一定のままです。`tool_use.scheme: category` を設定すると chat で opt-in できます(解決先の登録済み scheme 名は `universal-category`)。

**使いどころ：** ツールセットが非常に大きく / 高速に成長しており、すべてのアクションをフラットに列挙するとリクエストのトークンコストが高くなりすぎる場合。ラッパーで LLM 向けツールリストが一定に保たれます。

### `retrieval`

ツール上の RAG。カタログ全体を提示する代わりに**検索ツール**を提示し、LLM が検索するとマッチしたアクションだけが呼び出し可能なツールとして再提示されます。`embedding.enabled: true`（FP-0066 §7）+ 設定済みの埋め込みプロバイダーが必要です（検索はセマンティック）。加えて `embedding.index.actions`（既定 **on** — このスキームが読む ~10件のカタログindex。通常のケースでは追加設定不要、[`embedding.index`](../../reference/config/reyn-yaml.md#embedding-fields) 参照）も必要です。マッチングがセマンティックなため品質は埋め込みインデックスに依存し、安定した well-indexed カタログに適しています。

**使いどころ：** ツールセットが**非常に大きく**、全件を提示するとトークンコストが大きすぎる場合。検索で候補を絞ってから呼び出します。

**測定結果（弱モデル 4-way リフレッシュ）：** retrieval は単一ステップの読み取りや read→transform→write チェーンではクリーンです。しかし**読み取りが多いマルチファイル**タスクでは、弱いモデルが順次ファイルを読み、ラウンドごとの search→re-present オーバーヘッドで遅くなります（タイムアウト傾向）。*正確だが遅い*という特性でありチューニングコストです（上限なしなら同じタスクを完了します）。よって retrieval は**カタログスケールの opt-in であり、弱モデルのデフォルト代替ではありません**：`enumerate-all` が弱モデルの chat デフォルトのまま（比較でタスク完了率最高・最速終了）です。

### `CodeAct`

コードとしてのツール。LLM が短い Python スニペットを書き、ツール呼び出しはコード内の `tool(...)` 呼び出しとして行われます。スニペットは**サンドボックス化されたサブプロセス**で実行され、各コード内呼び出しは JSON ツール呼び出しと**同一のパーミッションゲートをラウンドトリップします**。CodeAct の呼び出しは同等の JSON 呼び出し以上に厳格なゲートを通り、さらにサンドボックスの封じ込めが加わります。

**使いどころ：** **弱い / 低コストモデル**を実行する場合。ツール使用をコードとして表現することが JSON ツール呼び出しを有意に上回るモデルに対して。

CodeAct は `enumerate-all` presentation を **`content_fence` transport**
上で表現したもの（FP-0066 P4, #3247）— `enumerate-all` と同じ全件フラット
カタログですが、モデルはネイティブ tool call の代わりにフェンス付きコードとして
呼び出しを表現します。`tool_use.scheme: enumerate-all` + `tool_use.transport:
content_fence`（後述）で選択します。独立した `codeact` scheme 名はありません。

### `universal-category` を `content_fence` で

2 つの軸は組み合わせられます: `category` presentation を `content_fence`
transport 上で表現することもできます。モデルは CodeAct と同じくフェンス付き
Python を書きますが、見せられる関数はカタログの**ラッパー**（`list_actions` /
`describe_action` / `invoke_action`）と base tools だけです。呼び出しは

```python
result = invoke_action(action_name="read_file", args={"path": "README.md"})
```

の形になり、code-API は**カタログと共に増えません** — `category` presentation
の存在理由そのものであり、transport を変えても保たれます。

**使いどころ:** 弱い / 低コストモデルで JSON tool call よりコードを書かせた方が
良く（`content_fence` の理由）、**かつ**カタログが大きく全アクションの列挙が
割に合わない場合（`category` の理由）。CodeAct は後者を捨てています（全アクション
を見せます）。`tool_use.scheme: category` + `tool_use.transport: content_fence`
で選択します。

### `retrieval` を `content_fence` で

3 つ目の組み合わせ: 検索起点の presentation を code-API として表現します。
モデルに見せる関数は `search_actions` / `describe_action` / `invoke_action` と
base tools で、**`list_actions` は含みません** — この presentation の discovery は
列挙ではなく検索であり、そこが上の `category` セルとの違いです:

```python
hits = search_actions(query="read a file")
result = invoke_action(action_name="read_file", args={"path": "README.md"})
```

★ **`tool_calls` 側の兄弟セルより 1 往復安い**というのが、この組を対称性のため
ではなく実利のために埋めた理由です。`tool_calls` では絞り込みに OS の再提示が
必要です（`tools=` ペイロードは LLM 呼び出しの *間* にしか変えられない — それが
`RePresent` の役割）。`content_fence` では検索結果はスニペット内の単なる値なので、
検索と呼び出しが同一ターンで完結します。そして `RePresent` は**使えません**:
この transport の tool-use サーフェスはシステムプロンプトそのもので、プロンプトは
1 ターンに 1 度だけ構築されるため、再提示された code-API には行き場がありません。

**使いどころ:** カタログが大きくカテゴリからのブラウズが入口として適切でなく、
かつ弱い / 低コストモデルで JSON tool call よりコードを書かせた方が良い場合。
他の `retrieval` セルと同様 `embedding.enabled: true`（加えて `embedding.index.actions`、既定 on — 上の `retrieval` 節を参照）が必要です。埋め込み
インデックスが未準備のときは、空を返す検索を見せる代わりにフラットカタログの
列挙へフォールバックします（`tool_calls` 側と同じ degrade・同じ理由 — インデックス
の無い検索はモデルを空結果でスタックさせます）。

`tool_use.scheme: retrieval` + `tool_use.transport: content_fence` で選択します。

## chat レイヤーの選択

tool-use は 2 つの config key に分解されます: `tool_use.scheme`（**presentation**
軸 — `category` / `enumerate-all` / `retrieval`）と `tool_use.transport`
（モデルが選択したアクションをどう表現するか — `tool_calls` / `content_fence`）。
これらの値の組み合わせはすべて実装済みです。reyn に存在しない `scheme` /
`transport` 名を指す組は、default にフォールバックせず config-parse 時に
大きく失敗します。

```yaml
# reyn.yaml
tool_use:
  scheme: enumerate-all       # トップレベル chat ルーター（デフォルト）
  transport: tool_calls       # デフォルト
```

CodeAct を選択するには:

```yaml
# reyn.yaml
tool_use:
  scheme: enumerate-all
  transport: content_fence
```

小サーフェスの code-API（`category` × `content_fence`）を選択するには:

```yaml
# reyn.yaml
tool_use:
  scheme: category
  transport: content_fence
```

検索起点の code-API（`retrieval` × `content_fence`）を選択するには:

```yaml
# reyn.yaml
tool_use:
  scheme: retrieval
  transport: content_fence
  # embedding.enabled: true が必要
```

旧 `tool_use.chat` の単一 key は #3247 で削除済み（clean-break、compat alias
無し）— これを書いた `reyn.yaml` は config-load 時に `scheme` / `transport`
への移行を名指すエラーで失敗します（黙って無視されることはありません）。
完全なキーリファレンス：[`reyn.yaml` § tool_use](../../reference/config/reyn-yaml.ja.md#tool_use-block)。

## スワップが安全な理由

スキームは*表現とパース*のみです。プラガブルなデータとして OS が読み込みます。本質的な部分はスキームの一部ではありません：

- LLM は OS が有効化したツール（候補セット）のみ呼び出せます。スキームはそれを広げることはできません。
- すべての呼び出しはディスパッチ前に除外 + パーミッションゲートを通ります。
- 呼び出しとその結果の検証は変更されません。

つまり `enumerate-all`、`retrieval`、`CodeAct` の選択はモデルのツール使用方法を変えるのみであり、許可されることは変わりません。表現は変わってもゲートは一定です。

## 参照

- [Universal Action Catalog](universal-catalog.md) — `universal-category` スキームの内部（step/phase のデフォルト、chat の代替）
- [`reyn.yaml` § tool_use](../../reference/config/reyn-yaml.ja.md#tool_use-block) — 設定リファレンス
- [Permission model](../runtime/permission-model.md) — すべてのスキームがディスパッチするゲート
