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
            "Transition to 'respond' with findings (string) and category='bug'."
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
            "This is a feature request. Write a short product spec: goal, proposed behaviour, open questions. "
            "Transition to 'respond' with findings (string) and category='feature_request'."
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
            "Transition to 'respond' with findings (string) and category='question'."
        ),
    ),
    "respond": Phase(
        name="respond",
        input_schema={
            "type": "object",
            "properties": {
                "findings": {"type": "string"},
                "category": {"type": "string"},
            },
            "required": ["findings", "category"],
        },
        instructions=(
            "Compose a polished customer-facing reply based on findings and category. "
            "Then finish the workflow."
        ),
    ),
}

app = App(
    name="support_app",
    entry_phase="triage",
    phases=phases,
    final_output_name="support_reply",
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
        },
        "required": ["reply", "category"],
    },
)
