---
type: phase
name: copy_to_work
input: improvement_session
role: workspace_initializer
max_act_turns: 0
allowed_ops: []
preprocessor:
  # Step 1: extract target skill name from the input artifact (pure dict/regex — safe mode).
  # Handles the improvement_session shape: data.target_skill is a short skill name
  # (e.g. "direct_llm"). Also handles top-level target_skill (OS runtime shape) and
  # user_message free-form text via regex. No filesystem access.
  - type: python
    module: ./copy_to_work.py
    function: extract_skill_name
    into: data._name
    output_schema:
      type: object
      properties:
        target_skill: {type: string}
      required: [target_skill]

  # Step 2a: OS-level skill resolution via skill_resolve run_op (R-PURE-MODE Class D).
  # Moves the fs walk (resolve_skill_path) into the OS layer. The `name` field is
  # populated at runtime via args_from (dot-path override) from data._name.target_skill
  # set by step 1. Never raises — resolution failure yields resolved=False with null fields.
  # on_error: skip so a missing skill surfaces as an informative error in step 2b rather
  # than aborting the preprocessor chain with an opaque OS error.
  - type: run_op
    op:
      kind: skill_resolve
      name: PLACEHOLDER
    args_from:
      name: data._name.target_skill
    into: data._resolved
    on_error: skip

  # Step 2b: pure dict transform — skill_resolve op output → resolve_paths shape.
  # Runs in safe mode (mode: safe): no fs I/O, no reyn imports. Derives all path strings
  # from data._resolved (set by step 2a) deterministically. Returns the same shape as
  # the legacy unsafe resolve_paths so downstream steps (steps 3–10) are unaffected.
  - type: python
    module: ./copy_to_work_resolver_pure.py
    function: resolve_paths_from_op
    into: data._prep
    output_schema:
      type: object
      properties:
        skill_glob:         {type: [string, "null"]}
        phases_glob:        {type: [string, "null"]}
        work_dir:           {type: [string, "null"]}
        original_skill_root:  {type: [string, "null"]}
        skill_slug:         {type: string}
        target_skill_path:  {type: [string, "null"]}
        target_skill_root:    {type: [string, "null"]}
        eval_spec_path:     {type: [string, "null"]}
      required: [skill_glob, phases_glob, work_dir, original_skill_root, skill_slug,
                 target_skill_path, target_skill_root, eval_spec_path]

  # Step 3: glob skill.md using the computed pattern
  - type: run_op
    op:
      kind: file
      op: glob
      path: PLACEHOLDER
    args_from:
      path: data._prep.skill_glob
    into: data._glob_skill

  # Step 4: glob phases/*.md using the computed pattern
  - type: run_op
    op:
      kind: file
      op: glob
      path: PLACEHOLDER
    args_from:
      path: data._prep.phases_glob
    into: data._glob_phases

  # Step 5: combine glob results into a copy plan (excludes eval.md)
  - type: python
    module: ./copy_to_work.py
    function: build_copy_plan
    into: data._copy_plan
    output_schema:
      type: array
      items:
        type: object
        properties:
          src: {type: string}
          rel: {type: string}
        required: [src, rel]

  # Step 6: read each source file
  - type: iterate
    over: data._copy_plan
    apply:
      type: run_op
      op:
        kind: file
        op: read
        path: PLACEHOLDER
      args_from:
        path: _iter.item.src
    into: data._reads
    on_error: fail

  # Step 7: pair read results with destination paths
  - type: python
    module: ./copy_to_work.py
    function: build_write_ops
    into: data._write_ops
    output_schema:
      type: array
      items:
        type: object
        properties:
          dst:     {type: string}
          content: {type: string}
        required: [dst, content]

  # Step 8: write each file to the work directory
  - type: iterate
    over: data._write_ops
    apply:
      type: run_op
      op:
        kind: file
        op: write
        path: PLACEHOLDER
        content: ""
      args_from:
        path: _iter.item.dst
        content: _iter.item.content
    into: data._write_results
    on_error: fail

  # Step 9: validate that all expected files were written
  - type: python
    module: ./copy_to_work.py
    function: validate_copy
    into: data.validation
    output_schema:
      type: object
      properties:
        ok:             {type: boolean}
        files_written:  {type: integer}
        files_expected: {type: integer}
        work_dir:       {type: string}
      required: [ok, files_written, files_expected, work_dir]

  # Step 10: inject resolved path fields into the session for downstream phases
  - type: python
    module: ./copy_to_work.py
    function: inject_resolved_paths
    into: data._resolved_paths
    output_schema:
      type: object
      properties:
        target_skill_path:  {type: string}
        target_skill_root:    {type: string}
        eval_spec_path:     {type: string}
        original_skill_root:  {type: string}
      required: [target_skill_path, target_skill_root, eval_spec_path, original_skill_root]
---

The preprocessor has deterministically resolved the skill path via the OS resolver and
copied all target skill DSL files to the work directory. Emit the updated session then
transition to run_and_eval.

CRITICAL — carry `_resolved_paths` verbatim: the emitted `improvement_session` artifact
MUST include `_resolved_paths` copied exactly from `data._resolved_paths`. Do NOT construct
path strings yourself. Do NOT omit this field — downstream phases (run_and_eval,
plan_improvements, apply_improvements, finalize) all depend on these OS-resolved paths.

The resolved paths in `data._resolved_paths` (copy all four verbatim):
- `target_skill_path` → work-dir copy of skill.md (downstream phases use this)
- `target_skill_root`   → work directory root
- `eval_spec_path`    → eval.md in the ORIGINAL skill directory (not copied to work)
- `original_skill_root` → original skill directory (finalize uses this for copy-back)

The computed work directory is available in `data._prep.work_dir` (same as
`_resolved_paths.target_skill_root`).
