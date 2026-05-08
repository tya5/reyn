# Research

Reyn Researcher ロールの成果物置き場。競合追跡・市場動向・優位性分析を
構造化して蓄積する。

## journal/insights との棲み分け

| 置き場 | 性格 |
|---|---|
| `docs/deep-dives/journal/insights/` | session 中に派生した技術的発見。observe-then-synthesize |
| `docs/deep-dives/research/competitive/` | 競合を主体的に調査した構造化レポート。Researcher ロール主導 |
| `docs/deep-dives/research/positioning/` | Reyn の優位性・差別化の整理（内向き strategic doc） |
| `docs/deep-dives/research/landscape/` | ecosystem / market 動向スナップショット |
| `docs/en/decisions/` | 設計判断の結論（ADR）。不変 |

## サブセクション

| Section | 内容 |
|---|---|
| [competitive/](competitive/) | 競合システム別の深掘り分析。`competitive/README.md` に横比較表 |
| [positioning/](positioning/) | Reyn の差別化・優位性の整理 |
| [landscape/](landscape/) | agent framework ecosystem / market 動向 |

## ファイル命名規則

- `competitive/`: `{competitor-name}.md`（日付なし、随時更新型）
- `landscape/`: `{topic}.md`（随時更新型）
- `positioning/`: `{topic}.md`（随時更新型）

随時更新型ファイルは frontmatter で `last_updated:` を管理する。

```yaml
---
title: ...
last_updated: 2026-05-08
status: draft | stable | outdated
---
```

## 作業メモ

調査の途中経過・一時ノートは `tmp/research/`（gitignored）に置き、
まとまったものだけ `docs/deep-dives/research/` に昇格する。
