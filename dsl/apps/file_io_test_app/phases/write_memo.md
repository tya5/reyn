---
type: phase
name: write_memo
input: write_request
input_description: A request specifying the filename, title, and topic for a memo to be generated and written to the workspace.
role: memo_writer
---

Generate a short memo based on data.title and data.prompt.
Write the memo to the workspace using control_ir.

Control IR instructions:
- Add exactly one entry to control_ir with kind="file", op="write".
- Use data.filename as the path (e.g. "memos/my_memo.txt").
- Set content to the full text of the generated memo.

The generated memo must include:
- A heading line with the title
- 2–3 paragraphs of body text based on data.prompt

After writing, set the artifact fields:
- filename: the path you wrote to (same as data.filename)
- char_count: the character count of the content you wrote
- summary: one sentence describing what the memo covers

Example control_ir:
[{"kind": "file", "op": "write", "path": "memos/hello.txt", "content": "# Title\n\nBody text..."}]
