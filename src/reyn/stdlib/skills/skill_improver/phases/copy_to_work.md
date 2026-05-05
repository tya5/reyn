---
type: phase
name: copy_to_work
input: improvement_session
role: workspace_initializer
max_act_turns: 0
allowed_ops: []
preprocessor:
  # Step 1: resolve target_skill → all derived paths via OS resolver (resolve_skill_path).
  # target_skill is a short skill name only (e.g. "direct_llm"); no path strings from LLM.
  # Runs in trusted mode (copy_to_work_resolver.py) because resolve_skill_path does
  # filesystem existence checks. Pure-mode functions live in copy_to_work.py.
  - type: python
    module: ./copy_to_work_resolver.py
    function: compute_paths
    into: data._prep
    output_schema:
      type: object
      properties:
        skill_glob:         {type: string}
        phases_glob:        {type: string}
        work_dir:           {type: string}
        original_dsl_root:  {type: string}
        skill_slug:         {type: string}
        target_skill_path:  {type: string}
        target_dsl_root:    {type: string}
        eval_spec_path:     {type: string}
      required: [skill_glob, phases_glob, work_dir, original_dsl_root, skill_slug,
                 target_skill_path, target_dsl_root, eval_spec_path]

  # Step 2: glob skill.md using the computed pattern
  - type: run_op
    op:
      kind: file
      op: glob
      path: PLACEHOLDER
    args_from:
      path: data._prep.skill_glob
    into: data._glob_skill

  # Step 3: glob phases/*.md using the computed pattern
  - type: run_op
    op:
      kind: file
      op: glob
      path: PLACEHOLDER
    args_from:
      path: data._prep.phases_glob
    into: data._glob_phases

  # Step 4: combine glob results into a copy plan (excludes eval.md)
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

  # Step 5: read each source file
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

  # Step 6: pair read results with destination paths
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

  # Step 7: write each file to the work directory
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

  # Step 8: validate that all expected files were written
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

  # Step 9: inject resolved path fields into the session for downstream phases
  - type: python
    module: ./copy_to_work.py
    function: inject_resolved_paths
    into: data._resolved_paths
    output_schema:
      type: object
      properties:
        target_skill_path:  {type: string}
        target_dsl_root:    {type: string}
        eval_spec_path:     {type: string}
        original_dsl_root:  {type: string}
      required: [target_skill_path, target_dsl_root, eval_spec_path, original_dsl_root]
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
- `target_dsl_root`   → work directory root
- `eval_spec_path`    → eval.md in the ORIGINAL skill directory (not copied to work)
- `original_dsl_root` → original skill directory (finalize uses this for copy-back)

The computed work directory is available in `data._prep.work_dir` (same as
`_resolved_paths.target_dsl_root`).
