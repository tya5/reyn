---
type: phase
name: copy_to_work
input: improvement_session
role: workspace_initializer
max_act_turns: 0
allowed_ops: []
preprocessor:
  # Step 1: compute all derived paths (slug, work_dir, glob patterns) from original_dsl_root
  - type: python
    module: ./copy_to_work.py
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
      required: [skill_glob, phases_glob, work_dir, original_dsl_root, skill_slug]

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
    into: data._validation
    output_schema:
      type: object
      properties:
        ok:             {type: boolean}
        files_written:  {type: integer}
        files_expected: {type: integer}
        work_dir:       {type: string}
      required: [ok, files_written, files_expected, work_dir]
---

The preprocessor has deterministically copied all target skill DSL files to the
work directory. Emit the updated session with `target_dsl_root` and
`target_skill_path` pointing to the work directory, then transition.

The computed work directory is available in `data._prep.work_dir`.
