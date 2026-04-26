---
type: phase
name: read_verify
input: file_written
input_description: Metadata about a memo that was written to the workspace, including its filename and character count.
role: verifier
can_finish: true
---

Verify that the memo written in the previous phase can be read back from the workspace.

Control IR instructions:
- Add exactly one entry to control_ir with kind="file", op="read".
- Use data.filename as the path.

Example control_ir:
[{"kind": "file", "op": "read", "path": "memos/hello.txt"}]

After issuing the read, set the artifact fields:
- filename: data.filename (same path)
- content_preview: the first 120 characters of the memo you generated in the previous step
  (reconstruct from data.summary if the read result is not available to you)
- char_count: data.char_count
- verified: true if char_count > 0, false otherwise
