---
type: phase
name: review
input: user_message
role: text_reviewer
can_finish: true
allowed_ops: []
preprocessor:
  - type: python
    module: ./stats.py
    function: compute_text_stats
    into: data.stats
    output_schema:
      type: object
      properties:
        char_count:        {type: integer, minimum: 0}
        word_count:        {type: integer, minimum: 0}
        line_count:        {type: integer, minimum: 0}
        longest_line_chars: {type: integer, minimum: 0}
        estimated_tokens:  {type: integer, minimum: 1}
      required: [char_count, word_count, line_count, longest_line_chars, estimated_tokens]
permissions:
  python:
    - module: ./stats.py
      function: compute_text_stats
      mode: pure
      timeout: 5
---

Write a short commentary on the user's text.

## Inputs

`input_artifact.data.text` — the original user message.

`input_artifact.data.stats` — precomputed deterministic statistics:

- `char_count` — total characters
- `word_count` — whitespace-separated word count
- `line_count` — number of lines
- `longest_line_chars` — chars in the single longest line
- `estimated_tokens` — rough LLM token estimate

These numbers were computed by a Python preprocessor; **trust them
verbatim**. Do not estimate or round — quote the exact values when
relevant.

## Output

`text_review.commentary` — a short paragraph (1–3 sentences) about
the input, weaving the stats in naturally. Examples:

- "133 chars across 3 lines; the second line is by far the longest at 87 chars."
- "Just 12 words and 1 line — fine for a quick prompt."
- "Roughly 480 estimated tokens; consider trimming if you're paying per-token."

Match the user's language (Japanese in / Japanese out, English in /
English out, etc.).

## Constraints

- Cite at least one stat verbatim. Don't say "around 130 chars" when
  `char_count` is 133.
- Don't recompute anything yourself — Python already has.
- Stay under ~4 lines.
