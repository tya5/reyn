---
type: skill
name: sample_skill
description: A deliberately under-specified summarizer for the improver demo.
entry: summarize
final_output: summary
final_output_description: A short summary of the user's text.
finish_criteria:
  - The summary phase produced an output
graph:
  summarize: []
---

## Overview

Summarize input text. Used as a target for `skill_improver` — kept
intentionally vague so the improver has something to refine.
