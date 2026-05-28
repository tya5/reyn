"""
shape_renderer — `shape_only` fenced-code-block annotation for the skill DSL.

Background
----------
Skill and phase .md files often include JSON example blocks that illustrate
the *shape* of an input artifact.  Without an explicit signal, weak LLMs
sometimes treat the example values as authoritative runtime data (= the
"literal-emission" defect class documented in FP-0008 PR-F/PR-J/PR-M).

The `shape_only` annotation lets skill authors write natural-looking example
JSON while the compiler automatically:

  1. Replaces every string value in the block with an uppercase placeholder
     derived from the field key  (``"instance_id": "django__django-12345"``
     → ``"instance_id": "<INSTANCE_ID_FROM_ARTIFACT>"``).
  2. Injects a "Critical — shape placeholders" warning paragraph before the
     block so the LLM knows the values are documentation-only.
  3. Strips the ``shape_only`` marker from the rendered info string (the
     block appears as plain ``json`` to the LLM).

Usage in a .md file
-------------------
Write the code fence opening as::

    ```json shape_only

instead of just::

    ```json

The content inside should be natural-looking JSON — the compiler transforms
it automatically.  Example::

    ```json shape_only
    {
      "type": "swe_bench_input",
      "data": {
        "instance_id": "django__django-12345",
        "repo": "django/django",
        "base_commit": "d16bfe05a744909de4b27f5875fe0d4ed41ce607",
        "problem_statement": "BUG: foo raises AttributeError ...",
        "hints_text": "Look at foo/bar.py",
        "test_patch": "diff --git a/tests/test_foo.py b/tests/test_foo.py\n..."
      }
    }
    ```

The LLM receives (after compiler transformation)::

    > **Critical — shape placeholders only**: The ``<*_FROM_ARTIFACT>``
    > values below document the *shape* of the data; they are NOT literal
    > values to copy into tool calls. Read the actual values from the
    > OS-injected input artifact at runtime.

    ```json
    {
      "type": "swe_bench_input",
      "data": {
        "instance_id": "<INSTANCE_ID_FROM_ARTIFACT>",
        "repo": "<REPO_FROM_ARTIFACT>",
        "base_commit": "<BASE_COMMIT_FROM_ARTIFACT>",
        "problem_statement": "<PROBLEM_STATEMENT_FROM_ARTIFACT>",
        "hints_text": "<HINTS_TEXT_FROM_ARTIFACT>",
        "test_patch": "<TEST_PATCH_FROM_ARTIFACT>"
      }
    }
    ```

Design notes
------------
- String replacement is JSON-level: only leaf string values are replaced.
  Non-string values (numbers, booleans, null, nested objects/arrays) are
  preserved as-is.
- Nested object keys produce UPPER_CASE placeholders from their own key
  name (not the full dot-path), consistent with the flat key pattern the
  LLM is already familiar with.
- Blocks that fail JSON parsing are left unchanged and a warning is logged;
  malformed JSON in a shape_only block is never silently destroyed.
- Blocks without the ``shape_only`` marker pass through completely
  unchanged (zero overhead for non-annotated files).
"""
from __future__ import annotations

import json
import logging
import re

_log = logging.getLogger(__name__)

# Matches ``` json shape_only (with optional spaces between tokens)
# and captures the body up to the matching closing ```.
# Group 1 = indentation/leading whitespace before the opening fence
# Group 2 = the JSON body (everything between opening and closing ```)
_SHAPE_ONLY_BLOCK_RE = re.compile(
    r"([ \t]*)```[ \t]*json[ \t]+shape_only[ \t]*\n"
    r"(.*?)"
    r"```",
    re.DOTALL,
)

_CRITICAL_WARNING = (
    "> **Critical — shape placeholders only**: The `<*_FROM_ARTIFACT>` "
    "values below document the *shape* of the data; they are **NOT** literal "
    "values to copy into tool calls.  "
    "Read the actual values from the OS-injected input artifact at runtime."
)


def _key_to_placeholder(key: str) -> str:
    """Convert a JSON key to an UPPER_CASE_FROM_ARTIFACT placeholder string.

    Examples::
        "instance_id"       → "<INSTANCE_ID_FROM_ARTIFACT>"
        "base_commit"       → "<BASE_COMMIT_FROM_ARTIFACT>"
        "problem_statement" → "<PROBLEM_STATEMENT_FROM_ARTIFACT>"
    """
    return f"<{key.upper()}_FROM_ARTIFACT>"


def _replace_string_values(obj: object) -> object:
    """Recursively replace every leaf string value with a key-derived placeholder.

    Dict keys are preserved; only leaf string values are replaced.
    Numbers, booleans, null, and nested containers are traversed but their
    non-string leaves are left unchanged.
    """
    if isinstance(obj, dict):
        return {k: (_key_to_placeholder(k) if isinstance(v, str) else _replace_string_values(v))
                for k, v in obj.items()}
    if isinstance(obj, list):
        # List items don't have keys; use a positional placeholder
        return [
            (f"<ITEM_{i}_FROM_ARTIFACT>" if isinstance(v, str) else _replace_string_values(v))
            for i, v in enumerate(obj)
        ]
    return obj


def _transform_shape_only_block(body: str) -> str:
    """Transform a shape_only JSON body: replace string values with placeholders.

    Returns the transformed JSON string, or the original body if it cannot be
    parsed as JSON (with a warning logged).
    """
    try:
        parsed = json.loads(body)
    except json.JSONDecodeError as exc:
        _log.warning(
            "shape_only block contains invalid JSON; leaving unchanged: %s", exc
        )
        return body

    transformed = _replace_string_values(parsed)
    try:
        return json.dumps(transformed, indent=2, ensure_ascii=False)
    except Exception as exc:  # noqa: BLE001  — best-effort; never crash the compiler
        _log.warning("shape_only block serialisation failed; leaving unchanged: %s", exc)
        return body


def _replace_block(match: re.Match) -> str:
    """Regex replacement callback for one ``shape_only`` code block."""
    indent = match.group(1)
    body = match.group(2)

    transformed_body = _transform_shape_only_block(body)

    warning = _CRITICAL_WARNING
    fence_open = "```json"
    fence_close = "```"

    # Re-indent warning and fence if the block was indented
    if indent:
        warning_lines = warning.splitlines()
        warning = "\n".join(indent + line for line in warning_lines)
        fence_open = indent + fence_open
        fence_close = indent + fence_close
        body_lines = transformed_body.splitlines()
        transformed_body = "\n".join(indent + line for line in body_lines)

    return f"{warning}\n\n{fence_open}\n{transformed_body}\n{fence_close}"


def render_shape_only_blocks(text: str) -> str:
    """Process all ``json shape_only`` fenced code blocks in *text*.

    For each block:
      - String leaf values in the JSON are replaced with
        ``<KEY_FROM_ARTIFACT>`` uppercase placeholders.
      - A "Critical — shape placeholders only" warning paragraph is prepended.
      - The ``shape_only`` marker is stripped (block renders as plain ``json``).

    Blocks without the ``shape_only`` marker pass through unchanged.
    This function is idempotent: calling it twice on the same text produces
    the same result as calling it once (already-transformed blocks contain
    no ``shape_only`` marker and are not re-processed).
    """
    return _SHAPE_ONLY_BLOCK_RE.sub(_replace_block, text)
