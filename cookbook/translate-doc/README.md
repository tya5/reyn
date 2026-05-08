# translate-doc

> ℹ️ Uses a custom skill bundled in this example dir
> (`./translate_doc/skill.md`). Drop into `reyn/local/translate_doc/` to
> make it globally available, or keep it scoped to this example.

Translate an English document into Japanese, preserving structure
(headings, code fences, lists). No stdlib `translate` skill ships in v1 —
this recipe defines a small one under `reyn/local/`.

## What this shows

- The minimum scaffolding for a single-phase skill.
- Using `--output-language ja` is **not** the right move for whole-doc
  translation: it sets the output language meta-instruction but doesn't
  guarantee structural preservation. A dedicated phase with explicit
  rules is more reliable.

## Setup

Copy the skill into your local skill area:

```bash
cp -r cookbook/translate-doc/translate_doc reyn/local/translate_doc
```

## Run

Pass the document text on stdin (cleanest for multi-line):

```bash
cat docs/en/guide/for-skill-authors/build-an-agent-team.md | reyn run translate_doc
```

Or inline for a short snippet:

```bash
reyn run translate_doc "Hello, world. This is a small paragraph to translate."
```

## Expected output

A `translated_document` artifact:

```json
{
  "language": "ja",
  "text": "こんにちは、世界。これは翻訳する短い段落です。",
  "structural_notes": [
    "preserved 0 code blocks",
    "preserved 0 headings"
  ]
}
```

## Files

- `translate_doc/skill.md` — single-phase skill, `entry: translate`.
- `translate_doc/phases/translate.md` — phase instructions (preserve
  structure, do not translate code).
- `translate_doc/artifacts/translated_document.yaml` — output schema.

## Notes

- The phase instructions deliberately avoid enumerating output fields
  (P8) — the OS injects `candidate_outputs` from the schema.
- Want bidirectional? Make a `direction` field on the input artifact
  ("en→ja" or "ja→en") and branch in the phase prompt — no graph change
  needed.

## See also

- [Tutorial: your first skill](../../docs/en/guide/getting-started/02-your-first-skill.md)
- [How-to: localize output](../../docs/en/guide/for-skill-authors/localize-output.md)
