---
type: reference
topic: config
audience: [human, agent]
applies_to: [reyn.yaml, skill.md, phases/*.md]
---

# Permissions

Reyn の Permission システムは、ファイルパス、シェル、MCP ツール、名前付きツール、Python preprocessor ステップへのアクセスをゲートします。デフォルトは保守的です。デフォルト外のものには、Skill レベルの宣言とユーザーの承認、またはプロジェクト全体の事前承認（`reyn.yaml`）が必要です。

## デフォルト付与（宣言不要）

| Op | スコープ |
|----|-------|
| `file.read` / `file.glob` / `file.grep` | プロジェクトルート（CWD）配下の任意のパス。 |
| `file.write` / `file.edit` / `file.delete` | `<CWD>/.reyn/` または `<CWD>/reyn/` 配下のみ。 |

これらのデフォルト外のものはすべて宣言が必要です。

## Skill の宣言（skill.md frontmatter の `permissions:`）

Phase レベルの `permissions:` は削除されました。すべての Permission 宣言は `skill.md` frontmatter に記載します — [skill-md.md](../dsl/skill-md.md#permissions-skill-level) を参照してください。Phase はスキルが宣言したものを継承します。

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

`shell: true` でも、ランタイムの起動に `--allow-shell` が必要です。そうでなければ op は `shell_not_allowed` を発行します。

### `mcp`、`tool`

Phase が呼び出せる MCP サーバー名 / 名前付きツール ID のリスト。

### `file.read` / `file.write`

デフォルトゾーン外のパス向け。各エントリーは以下を持ちます:

- `path` — 絶対パス、または CWD からの相対パス。`~` は展開されます。
- `scope` — `just_path`（この正確なパス）または `recursive`（このパスとその以下のすべて）。

`file.write` は `write`、`edit`、`delete` op をカバーします。

### `python`

`python` preprocessor ステップの（モジュール、関数）ごとの宣言。[`reference/dsl/preprocessor.md`](../dsl/preprocessor.md) を参照してください。

- `module`、`function` — 対応する preprocessor ステップと一致しなければなりません。
- `mode` — `safe`（サンドボックス）または `unsafe`（AST サンドボックスなし；`--allow-untrusted-python` が必要）。
- `timeout` — 親が子を SIGKILL するまでのウォールクロック秒数。デフォルト `30`。

## Web op（Tier 1 — デフォルト許可）

`web_search` と `web_fetch` は **Tier 1** です: 宣言なしでデフォルトで通過します。使用に際して `permissions:` エントリーは不要です（FP-0022）。

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

永続的な選択は `<skill>/<op>/<path>` をキーとして `.reyn/approvals.yaml` に記録されます（recursive 付与の場合は末尾に `/`）。外部 Skill は別の Skill の承認を再利用できません。キーは Skill スコープで、権限昇格を防ぎます。

## プロジェクト全体の事前承認（`reyn.yaml`）

```yaml
permissions:
  shell: allow
  file.write: allow         # すべての Skill のすべての write 系 op を付与
  python:
    safe: allow             # すべての safe モードの python ステップを自動承認
    unsafe: allow           # ランタイムの --allow-untrusted-python も必要
    allowed_modules:
      - math
      - statistics
      - mypackage
```

プロジェクトが信頼できる場合にのみ `allow` を使用してください。`ask`（デフォルト）はプロンプトを表示し、`deny` は拒否します。

## 非インタラクティブなラン（CI、eval）

`reyn eval` は非インタラクティブで実行されます。プロンプトはありません。承認は `reyn.yaml` または `.reyn/approvals.yaml`（例えば、最初にインタラクティブでターゲット Skill を実行して保存）に事前に準備されている必要があります。

## 確認と取り消し

```bash
reyn permissions list             # 保存された承認を表示
reyn permissions revoke <key>     # 承認を削除
```

## 関連情報

- [reyn-yaml.md](reyn-yaml.md) — 完全なプロジェクト設定
- [state-dir.md](state-dir.md) — `.reyn/approvals.yaml` の場所
- [リファレンス: skill.md](../dsl/skill-md.md) — Permission の宣言
- [リファレンス: control-ir](../runtime/control-ir.md) — Permission が必要な op
