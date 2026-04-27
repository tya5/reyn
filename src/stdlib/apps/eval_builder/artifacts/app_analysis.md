---
type: artifact
name: app_analysis
---

# Structured analysis of the target app, used to generate an eval spec.

app_dsl_path: string
  # Path to the target app's app.md as given by the user.

dsl_root: string
  # DSL root inferred from app_dsl_path (e.g. "dsl/").

app_name: string
  # The app's name from its app.md frontmatter.

judge_model: string
  # The model to use for LLM-as-judge. Copy verbatim from the ContextFrame `model` field
  # (a class name such as "standard", or a LiteLLM model string).
  # Do NOT invent a model name.

phase_order: string[]
  # Phases in execution order (entry → ... → can_finish phases).

test_cases:
  type: array
  items:
    type: object
    properties:
      name:
        type: string
        # snake_case identifier for the test case.
      input:
        type: string
        # Realistic input text to pass as user_message.
      rationale:
        type: string
        # Why this case is a good test (what scenario it covers).
    required: [name, input, rationale]

phase_eval_designs:
  type: array
  items:
    type: object
    properties:
      phase:
        type: string
        # Phase name.
      artifact_type:
        type: string
        # The artifact type this phase produces.
      schema:
        type: array
        items:
          type: string
        # Deterministic field assertions — evaluated by the OS without LLM.
        # Format: "field_path: type[, constraint...]"
        # Examples:
        #   "filename: string, min_length 1"
        #   "score: number, range 0.0-1.0"
        #   "items: array, min 1"
        #   "verified: boolean, equals true"
        #   "files_written: array, contains \"app.md\""
        # Rules:
        #   - Only use field names that actually exist in the artifact schema you read.
        #   - Use dot-notation for nested fields: "review_result.score: number, range 0.0-1.0"
        #   - 2–5 assertions per phase covering the key output fields.
      quality:
        type: array
        items:
          type: string
        # LLM-judged criteria — for semantic / content checks that cannot be expressed as type/value assertions.
        # Examples:
        #   "summary フィールドが書き込んだメモの内容を要約している"
        #   "body に asyncio の基本概念の説明が含まれている"
        # Rules:
        #   - Only include checks that genuinely require reading and understanding content.
        #   - Do NOT duplicate what schema assertions already cover (field existence, type, range).
        #   - 0–3 criteria per phase; prefer fewer, higher-signal criteria.
    required: [phase, artifact_type, schema, quality]

cross_phase_assertions: string[]
  # Equality checks between fields from two different phases.
  # Format: "phase_a.field_path == phase_b.field_path"
  # Examples:
  #   "write_memo.filename == read_verify.filename"
  #   "plan_app.app_name == build_app.app_name"
  # Include when a value produced by one phase MUST match a value in a later phase.
  # Leave empty ([]) if no such relationship exists.

final_schema: string[]
  # Schema assertions for the app's final_output artifact.
  # Same format as phase_eval_designs[].schema.

final_quality: string[]
  # Quality criteria for the app's final_output artifact.
  # Same format as phase_eval_designs[].quality.
