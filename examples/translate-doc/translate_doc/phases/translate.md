---
type: phase
name: translate
input_schema: user_message
---

## What

Translate the document text from English to natural, native-sounding
Japanese suitable for a technical reader.

## Rules

- Preserve all Markdown structure: headings, lists, tables, blockquotes.
- Do not translate the contents of fenced code blocks or inline code.
- Do not translate URLs, file paths, or identifiers (function names,
  CLI flags, env vars).
- Translate prose around code blocks normally.
- If the source contains a YAML front-matter block, leave keys untouched
  and translate values that are clearly natural-language descriptions
  (e.g. `description:`).

## When done

When the entire document has been rendered in Japanese with structure
intact and code untouched.
