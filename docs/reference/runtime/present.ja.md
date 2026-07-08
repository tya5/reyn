---
type: reference
topic: runtime
audience: [human, agent]
search_hints: [present op, present reference, presentation, data_ref, data_inline, blueprint, template, catalog component, table, keyvalue, list, code, diff, markdown, image, $bind, JSON pointer, presentations.yaml, presentations.entries, present ack, bindings_dropped, presented event, replay, recovery gate, expiry placeholder]
---

# present op & サーフェス リファレンス

**present レイヤ**のオペレータ/エージェント向けリファレンス——`present` op の引数、v1
コンポーネントカタログ、パスバインディング、名前付きテンプレート登録、op ack、`presented`
監査イベント、replay/rewind 挙動。*なぜ*（軸 B/C の課題、LLM は形・ユーザは中身の非対称性、
guard/renderer の分割）については [コンセプト: Present レイヤ](../../concepts/runtime/present.ja.md)
を参照。op は [Control IR](control-ir.ja.md) の op カタログにも現れる。

## `present` op

```json
{
  "kind": "present",
  "data_ref": ".reyn/cache/tool-results/2026-.../structured.json",
  "blueprint": {
    "component": "table",
    "rows": {"$bind": "/results"},
    "columns": [
      {"header": "Title",  "path": "/title"},
      {"header": "Author", "path": "/author"}
    ]
  }
}
```

**データソース**を厳密に1つ、**テンプレート**を厳密に1つ:

| 引数 | 型 | 備考 |
|---|---|---|
| `data_ref` | string | `data_inline` と **XOR**。任意の zone-readable パス。offload された `structured_ref` は `file.read` セマンティクスで**全値に再ハイドレート**される（LLM 可視プレビューからではない）。 |
| `data_inline` | any | `data_ref` と **XOR**。既に LLM コンテキストにある小さなデータ（便宜用）。 |
| `template` | string | `blueprint` と **XOR**。登録済み提示名（後述の登録を参照）。未知名はエラーでなくフォールバックチェーンへ落ちる。 |
| `blueprint` | object \| array | `template` と **XOR**。インライン宣言的コンポーネントツリー（単一ノードまたは上から下への並び）。 |

- **Tier 0**（`ask_user` の兄弟）、**fire-and-continue** — ユーザ（信頼の根）への提示に出力
  権限ゲートはなく、`ask_user` と違って run を**停止しない**。唯一のゲート: `data_ref` の
  読み取り権限は **`file.read` と同一**に解決される——`present` はエージェントの file op 以上を
  読めない（`present` 拒否 ⇔ `file.read` 拒否）。

## v1 カタログ（表示専用・非実行）

全コンポーネントは読み取り専用。blueprint ノードは `{"component": <name>, ...slots}`。

| コンポーネント | スロット |
|---|---|
| `text` | `text`（bind またはリテラル） |
| `markdown` | `text` — CommonMark として描画 |
| `code` | `text`, `language?` |
| `diff` | `text` — 統合 diff |
| `keyvalue` | `rows: [{label, value}]` |
| `table` | `rows`（bind → 配列）, `columns: [{header, path}]` |
| `list` | `items`（bind → 配列）, `item_path?`（項目単位パス） |
| `image` | `src`, `alt?` — v1 では `[image: <alt>]` という dim テキストのプレースホルダーのみ描画され、マルチモーダル配送経路へはまだルーティングされない |

v1 に**対話コンポーネント**（ボタン / フォーム）は存在しない。

### バインディング — `$bind` / JSON Pointer

データは **JSON Pointer (RFC 6901)** パスで構造的に結合される。

- `{"$bind": "/results/0/title"}` — ポインタ文字列。`""` は**文書全体**をバインド。
- `$bind` オブジェクトでないものは**リテラル**（例: `header` 文字列）。
- `table` の `columns[].path` と `list` の `item_path` は**行相対**（各反復行に対する相対）。

バインディング結果（§4）: パスヒット → バインド; パスミス → **ソフトスキップ** +
`path_not_found` 記録; 型不一致 → 強制変換（スカラ → `table` `rows` スロットは1行テーブル）+
`type_mismatch` 記録; guard による無害化/サイズ上限 → `guard_stripped` 記録。**全**バインディングが
ミスすると op は `all_bindings_missed` を報告しフォールバックチェーンへルーティングする——
決してハード失敗しない。

op 検証の構造ゲートは、インライン blueprint の**非カタログコンポーネント**や**非パス
バインディング**をハードエラー（`status="error"`）として拒否する——これはテンプレートのバグで
あり、ソフトなバインディングドロップとは区別される。

## 名前付きテンプレート登録（オペレータ専用）

名前付きテンプレートは **`presentations.yaml`**（`presentations.entries`）で登録される——
**オペレータ/設定アクション**である。install op はなく、LLM が書くのはインライン blueprint のみ。

```yaml
presentations:
  entries:
    search_results:
      blueprint:                              # 必須; インラインコンポーネントツリー
        - component: table
          rows: {"$bind": "/results"}
          columns:
            - {header: Author, path: /author}
            - {header: Title,  path: /title}
      description: "Search results table"      # 任意
      enabled: true                            # 任意, 既定 true
```

blueprint はロード時に検証され、`<project>/.reyn/config/presentations.yaml` レイヤはターン
境界でホットリロードする。全フィールド表 + マージ順:
[reyn.yaml § presentations](../config/reyn-yaml.md#presentations-block)。

## テンプレートフォールバック — 4段

何かが描画されるまで劣化する（決してハードエラーにならない）:

1. **登録 `template`** → 2. **インライン `blueprint`** → 3. **デフォルトビューア**（データ形から
合成: `list[dict]` → `table`、`dict` → `keyvalue`、スカラ → `text`、diff-sniff → `diff`）→
4. **ジェネリック**（構造化 → YAML を `text` へ、プレーンテキストはそのまま——常に描画）。

フォールバックは全ミスのテンプレートまたは未知のテンプレート名で発火する。ack は**要求された**
テンプレートの統計に加え、実際に描画した段を示す `note` を報告する。

## Ack（op 結果）

LLM の唯一のフィードバック——コンパクトかつ高シグナル:

```yaml
ok: true
bindings_resolved: 3
rows: 500
bindings_dropped:
  - {path: "/results/0/author", reason: path_not_found}
  # reason ∈ {path_not_found, type_mismatch, guard_stripped}
all_bindings_missed: false
note: "…"        # フォールバック段が描画した場合のみ存在
```

多数行での `path_not_found` → 「テンプレートがデータ形に合わない」; `type_mismatch` →
「パスは正しいがコンポーネントが違う」; `guard_stripped` → 「テンプレートのバグでなく guard に
よる無害化」。エージェントはデータを取り込まずに自己修正する。

## `presented` イベント（P6 監査）

すべての提示は `presented` イベントを1つ発行し、**ref + 統計のみを運び、内容バイトは決して
運ばない**:

| フィールド | 意味 |
|---|---|
| `data_ref` | ref パス、または `data_inline` 提示では `<inline-data>` |
| `template` | 登録名、またはインライン blueprint では `blueprint:<hash>`（blueprint バイトなし） |
| `surface` | リスト、例 `["inline-cui"]`（レンダラ未接続時は `["null"]`） |
| `ingested` | `none` \| `partial` \| `full` — **OS 計算**（データはインラインか、ref の事前 `read_file` がセッション前方に現れるか）、LLM 自己申告ではない |
| `bindings_resolved` | 解決されたバインディング数 |
| `bindings_dropped` | `[{path, reason}]` |
| `rows` | バインドされた行数 |

## Replay / rewind — キャッシュとしての提示

提示は**キャッシュ**であり、`presented` イベントが**真実**である。replay
(`reyn events <log>`) または rewind 時、`presented` イベントは**ベストエフォート**で再描画される:

- **ref がまだ読める** → 内容がデータの形から再合成され（イベントはバイトを保存していないため、
  呼び出し側の元インライン blueprint ではなくデフォルト/ジェネリックビューアを使う）サーフェスへ届く。
- **ref が消えた**（GC 済み・利用不可）、またはデータが**インライン**で永続化されていない →
  耐久的な `presented` 監査イベントを指す**期限切れプレースホルダ**。クラッシュにも古い描画にも
  ならない。

`present` は保持ウィンドウに何もピン留めしない; ref は既存のライフサイクルを保ち、会話履歴に
提示バイトは含まれない（圧縮すべき新しいものはない）。

### recovery-feature ゲート — 非該当

CLAUDE.md の **recovery-feature truncate-falsify ゲート**（WAL イベント由来の再構築 / PITR /
rewind-restore 状態を追加する PR は、再構築ソースが WAL 切り詰めを生き延びることを証明せよ）は
present レイヤには**該当しない**。ここでの replay は**権威的状態を再構築しない**: 既に耐久的な
ref のベストエフォート再描画、またはプレースホルダという**表示専用の射影**を生成し、`present` は
**recovery-core 状態を書かない**。`presented` イベントから回復可能な状態を導くものは何もない。
将来の改訂が `presented` イベントから権威的状態を再構築するようになれば、その PR は
truncate-falsify テストを arc 内で持たねばならない。

## 関連

- [コンセプト: Present レイヤ](../../concepts/runtime/present.ja.md)
- [Control IR](control-ir.ja.md) — カタログ内の op
- [reyn.yaml § presentations](../config/reyn-yaml.md#presentations-block) — 登録
- [Events](../../concepts/runtime/events.ja.md) — replay と監査ログ
