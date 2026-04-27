---
type: phase
name: write_memo
input: write_request
input_description: A request specifying the filename, title, and topic for a memo to be generated and written to the workspace.
role: memo_writer
---

Generate a short memo based on data.title and data.prompt, then write it to the workspace at data.filename using control_ir.

The memo must include:
- A heading line with the title
- 2–3 paragraphs of body text based on data.prompt

After writing, set char_count to the exact number of characters in the content you wrote (len of the string). Do not leave it as 0.
