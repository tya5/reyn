---
type: app
name: file_io_test_app
entry: write_memo
final_output: file_io_result
final_output_description: |
  Result of the file I/O test: filename written, a preview of the content,
  character count, and a boolean indicating whether read-back succeeded.
---

write_memo -> read_verify
