## Returning a conclusion to the outside

You do **not** need a callback convention. `pipeline_launch` renders
`input_template` against the event's template vars -- so a correlation id
carried in the URI reaches the pipeline -- runs async, and the result comes
back on this session's own inbox. A `shell` step is the write-back leg to the
external system. A worked `pipeline_launch` example lives in the Hooks
section of the `reyn_cheat_sheet` skill.

## Asking the human

MCP **elicitation** is installed per connection with a timeout and a
listener check (`src/reyn/mcp/connection_service.py`). Use it instead of
inventing a question channel.

**Sampling is a different primitive** (server asks the client for a model
completion). Check whether it is wired at all before designing around it.
