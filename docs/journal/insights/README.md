# Insights — accumulated technical findings

session を跨いで再利用可能な技術発見を **1 件 1 file** で記録する場所。

## 役割分担

| place | role |
|---|---|
| `docs/journal/dogfood/` | 各 batch の retrospective、 時限的、 N=5+ の prediction calibration |
| **`docs/journal/insights/`** | **session を跨ぐ再利用可能な技術発見、 long-term reusable** |
| `docs/en/concepts/` | 永続 design 原則 (= P1-P8、 architecture)、 timeless |
| `docs/en/contributing/` | process / pedagogy doc (= dogfood-discipline、 testing policy) |
| memory (`~/.claude/projects/.../`) | agent-personal 作業記憶、 user 個人の伝言 |

memory は **agent と user の私的な作業記憶**、 insights は **team / future
contributors への永続伝達**。 同じ topic でも:
- memory: "feedback_envelope_layer_fix.md = 次 session の自分への 1 行 reminder"
- insights: "envelope-layer-attractor-fix.md = チームへの methodology + evidence"

memory を読む権限が無い人 (= future contributor、 GitHub 訪問者) でも、
insights だけで Reyn の deep technical lesson にアクセスできる。

## Index

| date | title | status | related |
|---|---|---|---|
| 2026-05-07 | [envelope-layer attractor fix + mutation isolation methodology](2026-05-07-envelope-layer-attractor-fix.md) | stable | G12 Pattern E / commit `aab6be2` |
| 2026-05-07 | [industry tool discovery patterns (Anthropic / OpenAI / Tool RAG / MCP-Zero)](2026-05-07-industry-tool-discovery-survey.md) | stable | G23 follow-up / Wave A revert wave |

## How to add an insight

### Naming
- 1 file per insight、 frontmatter mandatory
- format: `YYYY-MM-DD-<short-slug>.md`
- slug は keyword で grep しやすい英語 (= title は JA でも slug は英語)

### Frontmatter
```yaml
---
title: <短い title>
discovered: YYYY-MM-DD
session-context: <wave / session の概要>
related-commits: [<commit hash>, ...]
related-giveup: [G12, G23]              # giveup-tracker IDs
related-memory: [<feedback file slug>, ...]
status: stable | provisional | obsolete
---
```

### Status
- `stable`: 検証済 + 再利用可能 (= N≥10 dogfood / multi-perspective verify 済)
- `provisional`: 推測寄り、 検証途中 (= reader は再検証 prerequisite)
- `obsolete`: 後続発見で superseded (= 該当 insight に「superseded by ...」 link)

### Index update
README.md の Index table は **手動 update**。 自動生成は意図的に避ける (=
future agent が「何が新規 insight か」 を脳内で classify する step を残す)。

## 既存記録との関係

### giveup-tracker (`docs/journal/dogfood/giveup-tracker.md`)
- 案件 (= 解決待ち / 撤退済の bug や design issue) の単位で管理
- insights は giveup-tracker の **「学び」 部分を独立 doc 化** したもの
- 1 つの giveup 案件が複数の insight を生むこともあれば、 cross-cutting な
  methodology insight が複数 giveup から抽出されることもある

### feedback memory (`memory/feedback_*.md`)
- agent 個人の作業 reminder (= "次 session の私" 向け)
- insights は同 topic でも **team-public version**: 詳細 evidence + 再現
  procedure + 関連 references を含む
- memory が "1 行で思い出す" 用、 insights が "0 から学ぶ" 用
