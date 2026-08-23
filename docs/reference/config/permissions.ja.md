---
type: reference
topic: config
audience: [human, agent]
applies_to: [reyn.yaml, skill.md, phases/*.md]
---

# Permissions

Reyn の Permission システムは、ファイルパス、シェル、MCP ツール、名前付きツール、Python preprocessor ステップへのアクセスをゲートします。デフォルトは保守的です。デフォルト外のものには、Skill レベルの宣言とユーザーの承認、またはプロジェクト全体の事前承認（`reyn.yaml`）が必要です。

## デフォルト付与（宣言不要）

読み書きできるパスの集合は `permissions.file.read` / `permissions.file.write` そのものです。何も書かなければ **schema の default** が効きます — 既定は設定の定義の一部であって、ランタイムのゲート内部に隠れた規則ではありません。したがって**実行を止める側と、モデルに「触ってよい範囲」を伝える側が同じ答えを読みます**。

| 軸 | schema default | 対象 |
|----|-------|-------|
| `file.read`（`file.glob` / `file.grep` も） | `<zone-root>` | zone root とその配下すべて。 |
| `file.write`（`file.edit` / `file.delete` も） | `<zone-root>/.reyn` | state ディレクトリ。ただし保護された除外（`.reyn/approvals.yaml`、および recovery-core の `.reyn/config/` / `.reyn/state/` プレフィックス — これらは専用の op 経由でのみ変更）を除く。⚠️ 既知のgap(#5173): ライブの承認ストアは `.reyn/approvals.jsonl`(#5153)へ移りましたが、除外リストは更新されていません。 |

`<zone-root>` は**リテラルではなく記号**です。値はエントリポイントが与える zone アンカー（`reyn chat` / `reyn web` では workspace base dir、`reyn pipe` / plugin install / registry bootstrap では project root、コンテナバックエンドではコンテナ内のリポジトリルート）で、**プロセス起動まで確定しない**ため、リテラルパスを default として書くことは原理的にできません。

### 集合の3つの書き方

| 設定 | 意味 |
|---|---|
| 未設定 | 上記の schema default |
| `file.read: deny` | 空集合。都度確認（JIT）も抑止されます |
| `file.read: [パス…]` | **その集合**。パスリスト自体がパーミッションなので、default に**追加されるのではなく置き換えます**。エントリは素のパスまたは `{path, scope}`。`reyn.yaml` の scope は `just_path` と明示しない限り `recursive` です |
| `file.read: allow` | この軸にパス制限なし |

解決された集合の外側は、単に拒否されるわけではありません。リクエストバスがあれば都度ユーザーに確認し（chat / 対話実行）、無ければ拒否します（ヘッドレス / eval）。**設定＝常設の集合、JIT＝アクセス単位の拡張**という二層構造です。

## Skill の宣言（skill.md frontmatter の `permissions:`）

Phase レベルの `permissions:` は削除されました。すべての Permission 宣言は `skill.md` frontmatter に記載します — skill-md.md を参照してください。Phase はスキルが宣言したものを継承します。

```yaml
---
type: skill
name: example
entry: main
final_output: result
permissions:
  shell: true
  mcp: [my_server]
  tool: [web_search]
  file:
    read:
      - path: ~/notes
        scope: recursive
    write:
      - path: /tmp/output
        scope: just_path
  python:
    - module: stats
      function: compute
      mode: safe
      timeout: 30
---
```

### `shell`

この Phase で `shell` Control IR op を有効にするには `true`。デフォルトはオフ。

### `mcp`、`tool`

Phase が呼び出せる MCP サーバー名 / 名前付きツール ID のリスト。

### `file.read` / `file.write`

デフォルトゾーン外のパス向け。各エントリーは以下を持ちます:

- `path` — 絶対パス、または CWD からの相対パス。`~` は展開されます。
- `scope` — `just_path`（この正確なパス）または `recursive`（このパスとその以下のすべて）。

`file.write` は `write`、`edit`、`delete` op をカバーします。

### `python`

`python` preprocessor ステップの（モジュール、関数）ごとの宣言。`reference/dsl/preprocessor.md` を参照してください。

- `module`、`function` — 対応する preprocessor ステップと一致しなければなりません。
- `timeout` — 親が子を SIGKILL するまでのウォールクロック秒数。デフォルト `30`。

Python ステップは常にサンドボックス化されます（AST allowlist + 制限された builtins）。`mode: unsafe` の宣言はロード時に拒否されます — 生の I/O は `run_op` ステップに分離するか、permission でゲートされた `reyn.api.safe.*` サーフェスを使用してください。

## Web op（Tier 1 — デフォルト許可）

`web_search` と `web_fetch` は **Tier 1** です: 宣言なしでデフォルトで通過します。使用に際して `permissions:` エントリーは不要です。

`reyn.yaml` でプロジェクト全体をブロックできます:

```yaml
permissions:
  web.search: deny   # すべての web_search op をブロック
  web.fetch: deny    # すべての web_fetch op をブロック
  web.fetch: allow   # 明示的に事前承認（ラン時プロンプトを完全スキップ）
```

これは Tier 2-3 op（`shell`、`mcp`）とは異なります。Tier 2-3 は op を試みる前に `skill.md` で明示的な宣言が必要です。

## 承認フロー（インタラクティブ）

Phase がデフォルト外の Permission を宣言すると、Reyn は単一の起動時プロンプトを表示します:

```
[approval] my_skill/file.write needs:
  /tmp/output (just_path)

  [y] allow this run only
  [j] persist approval for this exact path + skill
  [r] persist approval for the parent dir (recursive) + skill
  [N] deny
```

永続的な選択は `<skill>/<op>/<path>` をキーとして `.reyn/approvals.jsonl`(append-only ledger — #5153)に記録されます（recursive 付与の場合は末尾に `/`）。外部 Skill は別の Skill の承認を再利用できません。キーは Skill スコープで、権限昇格を防ぎます。

`file.read`/`file.write` については、キーが一致するだけでは決着しません(#5042): 承認済み path 自体の identity が初回使用時に束縛され、以降の使用のたびに再照合されます。承認対象を削除して同じ path に別のものを作り直した場合は、古い承認をそのまま引き継がず再度確認が求められます — 詳細は [Concepts: permission model](../../concepts/runtime/permission-model.ja.md) 参照。

## プロジェクト全体の事前承認（`reyn.yaml`）

```yaml
permissions:
  shell: allow
  file.write: allow         # すべての Skill のすべての write 系 op を付与
  python:
    safe: allow             # すべての safe モードの python ステップを自動承認
    allowed_modules:
      - math
      - statistics
      - mypackage
```

プロジェクトが信頼できる場合にのみ `allow` を使用してください。`ask`（デフォルト）はプロンプトを表示し、`deny` は拒否します。

## 非インタラクティブなラン（CI）

`reyn run-once` は非インタラクティブで実行されます。プロンプトはありません。承認は `reyn.yaml` または `.reyn/approvals.jsonl`（例えば、最初にインタラクティブで agent を実行して保存）に事前に準備されている必要があります。

## 確認と取り消し

```bash
reyn permissions list             # 保存された承認を表示
reyn permissions revoke <key>     # approved=False レコードをappend(履歴には残る)
```

## 関連情報

- [reyn-yaml.md](reyn-yaml.md) — 完全なプロジェクト設定
- [state-dir.md](state-dir.md) — `.reyn/approvals.jsonl` の場所
- リファレンス: skill.md — Permission の宣言
- [リファレンス: control-ir](../runtime/control-ir.md) — Permission が必要な op
