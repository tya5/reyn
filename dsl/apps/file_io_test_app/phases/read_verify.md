---
type: phase
name: read_verify
input: file_written
input_description: Metadata about a memo that was written to the workspace. filename is the path; char_count is the number of characters written; summary is a one-sentence description of the content.
role: verifier
can_finish: true
---

Read back the file at data.filename from the workspace using control_ir to verify it was written correctly.
Use data.char_count and data.summary to populate the output artifact fields defined in the candidate schema.
