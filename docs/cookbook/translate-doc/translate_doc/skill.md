---
type: skill
name: translate_doc
description: Translate an English document into Japanese, preserving headings, lists, and code blocks.
entry: translate
final_output: translated_document
final_output_description: |
  The Japanese rendering of the input document with structural elements preserved.
finish_criteria:
  - The translate phase produced a translated_document artifact
  - Code blocks and inline code are unchanged from the source
graph:
  translate: []
---

## Overview

Single-phase document translator. Input is a `user_message` containing the
document text; output is a `translated_document` with `language="ja"`.

Designed as a starting point — extend to other target languages by adding
a `direction` field on the input and branching in the phase prompt.
