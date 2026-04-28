---
type: eval
app: dsl/apps/file_io_test_app/app.md
dsl_root: dsl/
judge_model: gpt-4o
---

## case: basic_memo_write
input: "meeting_notes.txt というファイルに、今日のミーティングの議事録メモを書いてください。"

### phase: write_memo
schema:
- filename: string, min_length 1
- char_count: integer, min 1
- summary: string, min_length 1

quality:
- summary フィールドが書き込んだメモの内容を要約している

### phase: read_verify
schema:
- filename: string, min_length 1
- char_count: integer, min 1
- content_preview: string, min_length 1
- verified: boolean, equals true

### cross_phase
- write_memo.filename == read_verify.filename

### final
schema:
- filename: string, min_length 1
- verified: boolean, equals true
- content_preview: string, min_length 1
- char_count: integer, min 1

## case: japanese_content_memo
input: "daily_report.txt というファイルに、日本語で今日の業務報告を書いてください。"

### phase: write_memo
schema:
- filename: string, equals "daily_report.txt"
- char_count: integer, min 1
- summary: string, min_length 1

quality:
- summary が日本語の内容を要約している

### phase: read_verify
schema:
- filename: string, equals "daily_report.txt"
- char_count: integer, min 1
- content_preview: string, min_length 1
- verified: boolean, equals true

quality:
- content_preview に日本語の文字列が含まれている

### cross_phase
- write_memo.filename == read_verify.filename

### final
schema:
- filename: string, equals "daily_report.txt"
- verified: boolean, equals true
- content_preview: string, min_length 1
- char_count: integer, min 1

quality:
- content_preview に日本語の文字列が含まれている
