"""
support_app — branching phase example

triage ─→ debug   ─┐
       ─→ spec    ─┼─→ respond → finish
       ─→ answer  ─┘

- triage:  classify the issue as bug / feature_request / question
- debug:   root-cause analysis for bugs
- spec:    write a feature spec for feature requests
- answer:  answer general questions
- respond: compose the final customer reply (converging phase)
"""
from agent_os.models import App, Phase, AppGraph

phases = {
    "triage": Phase(
        name="triage",
        input_schema={
            "type": "object",
            "properties": {
                "user_message": {"type": "string"},
            },
            "required": ["user_message"],
        },
        instructions=(
            "Read the user's message and classify it as one of: bug, feature_request, question. "
            "Then transition to the matching phase:\n"
            "  - bug            → 'debug'\n"
            "  - feature_request → 'spec'\n"
            "  - question       → 'answer'\n"
            "Pass the original user_message in the artifact."
        ),
    ),
    "debug": Phase(
        name="debug",
        input_schema={
            "type": "object",
            "properties": {
                "user_message": {"type": "string"},
            },
            "required": ["user_message"],
        },
        instructions=(
            "This is a bug report. Identify the likely root cause and list 2-3 investigation steps. "
            "Then transition to 'respond' with your findings."
        ),
    ),
    "spec": Phase(
        name="spec",
        input_schema={
            "type": "object",
            "properties": {
                "user_message": {"type": "string"},
            },
            "required": ["user_message"],
        },
        instructions=(
            "This is a feature request. Write a short product spec: goal, proposed behaviour, and open questions. "
            "Then transition to 'respond' with your draft spec."
        ),
    ),
    "answer": Phase(
        name="answer",
        input_schema={
            "type": "object",
            "properties": {
                "user_message": {"type": "string"},
            },
            "required": ["user_message"],
        },
        instructions=(
            "This is a general question. Write a clear, concise answer. "
            "Then transition to 'respond' with your answer."
        ),
    ),
    "respond": Phase(
        name="respond",
        input_schema={
            "type": "object",
            "properties": {
                "analysis": {"type": "string"},
                "action_items": {
                    "type": "array",
                    "items": {"type": "string"},
                },
            },
            "required": ["analysis", "action_items"],
        },
        instructions=(
            "Compose a polished customer-facing reply based on the analysis and action items. "
            "Then finish the workflow."
        ),
    ),
}

app = App(
    name="support_app",
    entry_phase="triage",
    phases=phases,
    graph=AppGraph(
        transitions={
            "triage": ["debug", "spec", "answer"],
            "debug":  ["respond"],
            "spec":   ["respond"],
            "answer": ["respond"],
            "respond": [],
        },
        can_finish_phases=["respond"],
    ),
    final_output_schema={
        "type": "object",
        "properties": {
            "reply": {"type": "string"},
            "category": {"type": "string"},
            "action_items": {
                "type": "array",
                "items": {"type": "string"},
            },
        },
        "required": ["reply", "category", "action_items"],
    },
)
