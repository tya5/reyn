# Journal

Reyn の開発の **過程** を残す場所。 「何が決まったか」 (= ADR) でも
「どう書くか」 (= reference) でもなく、 「**何が起きたか**」 を物語として
残す。

ADR と journal の違い:

- **ADR** (`docs/en/decisions/`): 設計判断の **結論**。 不変。 「我々はこう
  決めた」
- **Journal**: 結論に至る **過程**、 もしくは結論に乗らない **observation**。
  「こういうことがあった」

両者を行き来して reyn の歴史を追えるようにする。

## サブセクション

| Section | 内容 |
|---|---|
| [dogfood/](dogfood/) | Reyn を Reyn で使った記録。 batch 単位、 finding 含む |
| [insights/](insights/) | session を跨ぐ再利用可能な技術発見。 1 件 1 file、 long-term reusable |
| [feature-verify/](feature-verify/) | 特定機能の verify ログ |

## 書き方

- 真面目な技術内容 + 読み物として面白い文章 = 両立を狙う
- ユーモアは控えめに、 だが章タイトル / 比喩 / 「予想 vs 実際」 構造で
  読者を引き込む
- 引用 / 数値 / commit hash は正確に。 物語性のために事実を曲げない

## 過去の議論記録

dogfood 以外の議論記録 (= 設計対話のフロー):

- `tmp/discuss_postprocessor.md` (gitignored、 開発記専用) — postprocessor
  設計議論の流れ。 user の指摘で 4 回ひっくり返った話を含む。 内容上 push
  されないが、 開発者間で参照可能

将来 push されない private な議論記録は `tmp/`、 push される dev chronicle
として残すものは `docs/journal/` という棲み分け。
