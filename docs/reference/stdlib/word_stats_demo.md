---
type: reference
topic: stdlib
audience: [human, agent]
applies_to: [word_stats_demo]
---

# `word_stats_demo`

Demo skill showing the `python` preprocessor step pattern. Produces a short commentary grounded in precomputed text statistics.

## Entry

`review`

## Final output

`text_review` — `commentary` (1–3 sentence prose citing the exact precomputed values).

## How it composes

A single `review` phase. The `stats.py:compute_text_stats` preprocessor (safe mode, 5 s timeout) runs deterministically before the LLM, replacing `data` with `{"text": ..., "stats": {"char_count", "word_count", "line_count", "longest_line_chars", "estimated_tokens"}}`. The LLM writes commentary citing the exact values — it does not recount anything.

## Caveats

Safe-mode Python; no special permissions required beyond the default. This skill is a reference example, not a production utility.

## Usage

```bash
reyn run word_stats_demo "任意のテキストを入れる"
```

## Source

[`src/reyn/stdlib/skills/word_stats_demo/skill.md`](https://github.com/tya5/reyn/blob/main/src/reyn/stdlib/skills/word_stats_demo/skill.md)
