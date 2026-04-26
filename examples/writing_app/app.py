"""
writing_review_app — review/judge separation example

Flow:
  analyze → draft → review → judge → finish
                           ↘ revise → review → judge → ...

Artifact types:
  analyze  → analysis_result
  draft    → draft_article
  review   → review_package  {article, review_result}   # carries body forward to judge
  judge    → judge_decision  {decision, reason, confidence, article, revision_notes}
           OR finish (with reason + confidence at top level)
  revise   → revised_article

Responsibilities:
  review : evaluate the article (no transition decision)
  judge  : decide finish or revise (no evaluation)
  revise : rewrite according to revision_notes
"""
from agent_os.models import App, Phase, AppGraph

phases = {
    "analyze": Phase(
        name="analyze",
        input_schema={
            "type": "object",
            "properties": {
                "topic": {"type": "string"},
                "audience": {"type": "string"},
                "tone": {
                    "type": "string",
                    "enum": ["casual", "academic", "persuasive"],
                },
            },
            "required": ["topic", "audience"],
        },
        instructions=(
            "Analyze the topic for the given audience. "
            "Identify 3-5 key points, choose an editorial angle, and infer tone if not provided. "
            'Produce: {"type":"analysis_result","data":{"key_points":[...],"angle":"...","tone":"..."}}.'
        ),
    ),

    "draft": Phase(
        name="draft",
        input_schema={
            "type": "object",
            "properties": {
                "type": {"type": "string", "const": "analysis_result"},
                "data": {
                    "type": "object",
                    "properties": {
                        "key_points": {
                            "type": "array",
                            "items": {"type": "string"},
                            "minItems": 3,
                            "maxItems": 5,
                        },
                        "angle": {"type": "string"},
                        "tone": {"type": "string"},
                    },
                    "required": ["key_points", "angle"],
                },
            },
            "required": ["type", "data"],
        },
        instructions=(
            "Write a draft article based on key_points, angle, and tone from data. "
            'Produce: {"type":"draft_article","data":{"title":"...","body":"...","self_assessment":"..."}}.'
        ),
    ),

    "review": Phase(
        name="review",
        input_schema={
            "type": "object",
            "properties": {
                "type": {
                    "type": "string",
                    "enum": ["draft_article", "revised_article"],
                },
                "data": {
                    "type": "object",
                    "properties": {
                        "title": {"type": "string"},
                        "body": {"type": "string"},
                        "self_assessment": {"type": "string"},
                    },
                    "required": ["title", "body"],
                },
            },
            "required": ["type", "data"],
        },
        instructions=(
            "Evaluate the article (title + body) in data against the finish_criteria. "
            "Your ONLY job is evaluation — do NOT decide to finish or revise. "
            "Carry the article forward so the next phase can act on it. "
            "Produce: "
            '{"type":"review_package","data":{'
            '"article":{"title":"...","body":"..."},'
            '"review_result":{"strengths":[...],"issues":[...],"score":0.0-1.0,"quality_notes":[...]}'
            "}}."
        ),
    ),

    "judge": Phase(
        name="judge",
        input_schema={
            "type": "object",
            "properties": {
                "type": {"type": "string", "const": "review_package"},
                "data": {
                    "type": "object",
                    "properties": {
                        "article": {
                            "type": "object",
                            "properties": {
                                "title": {"type": "string"},
                                "body": {"type": "string"},
                            },
                            "required": ["title", "body"],
                        },
                        "review_result": {
                            "type": "object",
                            "properties": {
                                "strengths": {
                                    "type": "array",
                                    "items": {"type": "string"},
                                },
                                "issues": {
                                    "type": "array",
                                    "items": {"type": "string"},
                                },
                                "score": {"type": "number"},
                                "quality_notes": {
                                    "type": "array",
                                    "items": {"type": "string"},
                                },
                            },
                            "required": ["score"],
                        },
                    },
                    "required": ["article", "review_result"],
                },
            },
            "required": ["type", "data"],
        },
        instructions=(
            "Decide whether to finish or revise based on the review_result and finish_criteria. "
            "Do NOT re-evaluate — only make the transition decision.\n"
            "\n"
            "Convergence rules (apply in priority order):\n"
            "1. score >= 0.8 → strongly prefer finish.\n"
            "2. current_phase_visit >= max_phase_visit - 1 → strongly prefer finish; "
            "   avoid further revision unless a critical issue exists.\n"
            "3. remaining issues are minor tweaks → prefer finish.\n"
            "4. Do NOT optimize indefinitely. Diminishing returns justify finishing.\n"
            "\n"
            "Choose the candidate that matches your decision:\n"
            "- finish (end candidate): use data.article.title and data.article.body. "
            "Summarize the review outcome concisely.\n"
            "- revise (revise candidate): carry data.article forward unchanged. "
            "Limit revision_notes to 3 actionable items."
        ),
    ),

    "revise": Phase(
        name="revise",
        input_schema={
            "type": "object",
            "properties": {
                "type": {"type": "string", "const": "judge_decision"},
                "data": {
                    "type": "object",
                    "properties": {
                        "article": {
                            "type": "object",
                            "properties": {
                                "title": {"type": "string"},
                                "body": {"type": "string"},
                            },
                            "required": ["title", "body"],
                        },
                        "revision_notes": {
                            "type": "array",
                            "items": {"type": "string"},
                            "minItems": 1,
                        },
                    },
                    "required": ["article", "revision_notes"],
                },
            },
            "required": ["type", "data"],
        },
        instructions=(
            "Rewrite the article (title + body in data.article) according to data.revision_notes. "
            "Address every revision note. "
            'Produce: {"type":"revised_article","data":{"title":"...","body":"...","self_assessment":"..."}}.'
        ),
    ),
}

app = App(
    name="writing_review_app",
    entry_phase="analyze",
    phases=phases,
    graph=AppGraph(
        transitions={
            "analyze": ["draft"],
            "draft":   ["review"],
            "review":  ["judge"],
            "judge":   ["revise"],
            "revise":  ["review"],
        },
        can_finish_phases=["judge"],
        max_phase_visits={
            "review": 3,
            "judge":  3,
            "revise": 3,
        },
    ),
    final_output_schema={
        "type": "object",
        "properties": {
            "title": {"type": "string"},
            "body": {"type": "string"},
            "quality_notes": {
                "type": "array",
                "items": {"type": "string"},
            },
        },
        "required": ["title", "body"],
    },
    finish_criteria=[
        "audience_fit",
        "clarity",
        "specificity",
        "structure",
        "language_consistency",
    ],
)
