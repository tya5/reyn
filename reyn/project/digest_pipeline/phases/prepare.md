---
type: phase
name: prepare
input: user_message
input_description: User's free-text request specifying topic and target audience.
role: request_parser
can_finish: false
---

Parse the user's request and extract:
- topic: the subject to write about
- audience: who the article is for
- tone (optional): desired tone (e.g. formal, casual, technical)

If audience is not specified, infer a reasonable one from the topic.
