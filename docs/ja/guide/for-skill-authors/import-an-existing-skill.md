---
type: how-to
topic: stdlib
audience: [human]
applies_to: [reyn/local/, reyn/project/]
---

# 既存の Skill をインポートする

**目的:** 他の場所で定義された Skill（プロンプト、小さなスクリプト、別の agent フレームワークのスペック）を Reyn の DSL に取り込む。

## 使うべき状況

- 別のツールで動作するプロンプトやワークフローがあり、その動作を保ちながら Reyn の構造（検証、Events、リプレイ）を活用したい。
- ワンショットプロンプトを複数 Phase の Reyn Skill として再構築しているが、出発点となるドラフトが欲しい。

## `skill_importer` stdlib Skill を使う

```bash
reyn run skill_importer "<既存のプロンプトまたはワークフローの説明をペースト>"
```

`skill_importer` は入力を読み取り、Phase グラフを推測して、ドラフトの Skill を `reyn/local/<name>/` に書き込みます。成功を宣言する前に `lint` Control IR op を使って出力を検証します。

## ワークフロー

1. **ペーストまたは説明。** `skill_importer` に生のプロンプトテキストまたは Skill が何をすべきかの説明を与えます。
2. **ドラフトをレビューする。** インポーターは `reyn/local/<name>/` 配下に `skill.md`、`phases/*.md`、`artifacts/*.yaml` を書き込みます。
3. **リント。** `reyn lint <name>` がクリーンになるべきです。そうでない場合、インポーターはランの終わりに問題を報告します。
4. **実行。** `reyn run <name> "<サンプル入力>"`。出力が正しくなければ Phase の指示をイテレートします。
5. **プロモート。** 満足したら、`reyn/local/` から `reyn/project/` に移動してチェックインします。

## インポーターのマッピング

| ソースのコンセプト | Reyn の相当物 |
|----------------|-----------------|
| 単一プロンプト | 1 Phase + Skill グラフ `entry → end` |
| 「ステップ 1、次にステップ 2」 | 線形に接続された複数 Phase |
| 「X なら Y、でなければ Z」 | 分岐 Phase（`triage → [branchA, branchB]`） |
| ツール呼び出し（ファイル読み取り、検索） | Control IR op |
| 繰り返しの構造化出力 | artifact スキーマ |

インポーターは常に最もクリーンな分解を選ぶとは限りません。インポート後は `skill_improver` で改善してください:

```bash
reyn run skill_improver "improve <name>" --allow-shell
```

## プロモーションチェックリスト

`reyn/project/` に移動する前に:

- [ ] `reyn lint <name>` がクリーン。
- [ ] 少なくとも 1 つのハッピーパス eval ケースが通過。
- [ ] `final_output_schema` が呼び出し元が実際に必要とするものと一致している。
- [ ] Phase の指示が P8 に従っている（スキーマの列挙なし、Control IR 構文なし）。

## 関連情報

- [リファレンス: stdlib/skill_importer](../../reference/stdlib/skill_importer.md)
- [リファレンス: stdlib/skill_improver](../../reference/stdlib/skill_improver.md)
- [リファレンス: lint CLI](../../reference/cli/lint.md)
- [agent: skill_importer mapping rules](../../../en/guide/for-skill-authors/skill-importer-mapping.md)
