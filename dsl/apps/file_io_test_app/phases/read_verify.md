---
type: phase
name: read_verify
input: file_written
input_description: Metadata about a memo that was written to the workspace, including its filename and character count.
role: verifier
can_finish: true
---

Read back the file at data.filename from the workspace using control_ir to verify it was written correctly.

Set the artifact fields:
- filename: data.filename
- content_preview: the first 120 characters of the memo content
  (use data.summary if the read result is not directly available)
- char_count: data.char_count
- verified: true if char_count > 0, otherwise false
