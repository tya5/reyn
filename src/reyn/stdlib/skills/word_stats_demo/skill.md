---
type: skill
name: word_stats_demo
description: |
  Demo of the python preprocessor step: a Python function computes
  text statistics (char count, line count, etc.) deterministically,
  then the LLM uses those numbers to comment on the input.
entry: review
final_output: text_review
final_output_description: |
  A short LLM-generated commentary that grounds itself in the
  Python-computed statistics rather than estimating them.
finish_criteria:
  - The Python preprocessor populated stats from the input text
  - The LLM produced a commentary referring to the computed numbers
graph:
  review: []
permissions:
  python:
    - module: ./stats.py
      function: compute_text_stats
      mode: pure
      timeout: 5
---

## Overview

A minimal demonstration of the `python` preprocessor step. The phase
declares a Python function that runs in pure mode (sandboxed), computes
deterministic text statistics, and injects them into the artifact under
`data.stats`. The LLM then writes a commentary that references those
exact numbers — something LLMs are otherwise unreliable at.

## Input

`user_message` text — any string.

## Output

`text_review.commentary` — short prose discussing the input through the
lens of the precomputed statistics.

## Why this is a good fit for python

LLMs are bad at counting characters / tokens / lines accurately. Python
counts them precisely in microseconds. By doing the count in Python and
showing the result to the LLM, the commentary stays factual.
