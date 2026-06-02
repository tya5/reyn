---
type: how-to
topic: config
audience: [human]
applies_to: [reyn.yaml, .reyn/approvals.yaml, phases/*.md]
---

# Permission を管理する

**目的:** 過剰に信頼を広げることなく、Skill に適切なケイパビリティを付与し、後から承認を確認・取り消す。

## Permission を設定する 3 か所

| レイヤー | 格納先 | 粒度 |
|-------|----------|-------------|
| Phase の宣言 | Phase の frontmatter | Phase ごと + (op, パス) ごと |
| 保存された承認 | `.reyn/approvals.yaml` | (Skill, op, パス) ごと |
| プロジェクト全体の事前承認 | `reyn.yaml` の `permissions:` | op kind ごと |

デフォルトは保守的です。その他はすべてオプトインです。理由については [Permission モデルのコンセプト](../../concepts/runtime/permission-model.md) を参照してください。

## Phase で宣言する

```yaml
---
type: phase
name: writeout
input: report
permissions:
  shell: false
  file:
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

`scope: just_path` は正確なパスに一致します。`recursive` はディレクトリとそのすべての子孫に一致します。

## 起動時に承認する

Skill がデフォルトにない何かを必要とする場合、ランタイムはプロンプトを表示します:

```
[approval] my_skill/file.write needs:
  /tmp/output (just_path)

  [y] allow this run only
  [j] persist for this exact path + skill
  [r] persist for the parent dir (recursive) + skill
  [N] deny
```

`j` と `r` は `.reyn/approvals.yaml` に書き込みます。

## プロジェクト全体で事前承認する

```yaml
# reyn.yaml
permissions:
  shell: allow
  file.write: allow
  python:
    safe: allow
    unsafe: allow      # ランタイムの --allow-unsafe-python も必要
```

`allow` はプロンプトを完全に削除します。`ask`（デフォルト）はプロンプトを表示します。`deny` は拒否します。

## 保存された承認を確認する

```bash
reyn permissions list
```

出力は Skill ごと、次に op kind ごとにエントリーをグループ化します:

```
  [my_skill]
    ✓ write  /tmp/output  (just_path)
    ✓ read   ~/notes      (recursive)
```

## 取り消す

```bash
reyn permissions revoke my_skill/file.write//tmp/output
reyn permissions clear     # すべて削除（確認を求める）
```

## eval モード

`reyn eval` は非インタラクティブです。ターゲット Skill が必要とするすべての承認を事前に準備してください:

- `reyn run` でターゲットを一度実行し、`[j]` または `[r]` で永続化する、または
- `reyn.yaml` で事前承認する。

事前承認がない場合、eval ケースは未完了として報告されます。

## 関連情報

- [リファレンス: permissions](../../reference/config/permissions.md)
- [リファレンス: reyn.yaml](../../reference/config/reyn-yaml.md)
- [リファレンス: state-dir](../../reference/config/state-dir.md)
- [コンセプト: Permission モデル](../../concepts/runtime/permission-model.md)
