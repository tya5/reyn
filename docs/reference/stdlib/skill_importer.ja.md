---
type: reference
topic: stdlib
audience: [human, agent]
applies_to: [skill_importer]
---

# `skill_importer`

公開 Skill レジストリを検索し、ユーザーに候補を選ばせ、選択した Skill を `reyn/local/` 配下にマルチ Phase の reyn Skill としてインポートします。

## エントリー

`search`

## 最終出力

`skill_import_result` — インストールパス、ソース URL、書き込んだファイルのリスト。

## プロベナンス

インポートされた Skill の `skill.md` には `imported_from`、`imported_at`、`imported_format`、`imported_revision` フィールドが付与されます。これらは非活性フィールドでパーサーには無視されますが、ソースへの追跡に役立ちます。

## 例

```bash
reyn run skill_importer "find a markdown summarizer skill"
```

## ソース

[`src/reyn/stdlib/skills/skill_importer/skill.md`](https://github.com/tya5/reyn/blob/main/src/reyn/stdlib/skills/skill_importer/skill.md)
