"""
App-node execution: run a sub-app and adapt its output to the parent's schema.

Standalone functions — dependencies are passed explicitly so this module
has no circular imports and stays testable in isolation.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable

from reyn.events.events import EventLog
from reyn.llm.llm import proxy_kwargs
from reyn.llm.model_resolver import ModelResolver
from reyn.schemas.models import SkillNodeSpec
from reyn.llm.pricing import TokenUsage


async def _adapt_artifact(
    data: dict,
    source_type: str,
    target_schema: dict,
    target_type: str,
    node_id: str,
    output_language: str | None,
    model: str,
    resolver: ModelResolver,
    events: EventLog,
    *,
    llm_timeout: float = 60.0,
    llm_max_retries: int = 3,
) -> tuple[dict, TokenUsage]:
    """
    Call LLM to convert a sub-app's final_output data to the parent's target schema.
    Returns (adapted_artifact, token_usage).
    """
    import litellm

    prompt_lines = [
        "Convert the following data to the target schema.\n",
        f"Source (type: {source_type}):",
        json.dumps(data, ensure_ascii=False, indent=2),
        "",
        "Target schema:",
        json.dumps(target_schema, ensure_ascii=False, indent=2),
        "",
        f'Produce a JSON object with "type" set to "{target_type}" and '
        f'"data" populated from the source, mapped to the target schema fields.',
    ]
    # Only emit the output-language directive when the caller (or top-level
    # config) actually specified one; otherwise the LLM picks language
    # based on the source data naturally. Reyn does not silently default
    # to a regional language for users who haven't configured one.
    if output_language:
        prompt_lines.append(f"Output language: {output_language}")
    prompt = "\n".join(prompt_lines)
    response = await litellm.acompletion(
        model=resolver.resolve(model).model,
        messages=[{"role": "user", "content": prompt}],
        response_format={"type": "json_object"},
        timeout=llm_timeout,
        num_retries=llm_max_retries,
        **proxy_kwargs(),
    )
    raw = json.loads(response.choices[0].message.content)
    usage = TokenUsage()
    if response.usage:
        usage = TokenUsage(
            prompt_tokens=response.usage.prompt_tokens,
            completion_tokens=response.usage.completion_tokens,
        )
    events.emit(
        "skill_node_adapted",
        node=node_id,
        source_type=source_type,
        target_type=target_type,
    )
    return raw, usage


async def execute_skill_node(
    node_id: str,
    node_spec: SkillNodeSpec,
    input_artifact: dict,
    target_schema: dict,
    target_type: str,
    output_language: str | None,
    *,
    model: str,
    strict: bool,
    subscribers: list[Callable],
    resolver: ModelResolver,
    events: EventLog,
    limits: Any = None,
) -> tuple[dict, TokenUsage]:
    """
    Run a sub-app to completion and adapt its final_output to target_schema.
    Returns (adapted_artifact, accumulated_token_usage).
    """
    from reyn.compiler import load_dsl_skill
    from reyn.kernel.runtime import OSRuntime

    events.emit("skill_node_started", node=node_id, skill_path=node_spec.skill_path)

    sub_skill = load_dsl_skill(node_spec.skill_path, dsl_root=node_spec.dsl_root)

    sub_runtime = OSRuntime(
        sub_skill,
        model=model,
        strict=strict,
        subscribers=subscribers,
        resolver=resolver,
        limits=limits,
    )
    run_result = await sub_runtime.run(input_artifact, output_language=output_language)
    token_usage = sub_runtime._token_usage

    events.emit(
        "skill_node_completed",
        node=node_id,
        status=run_result.status,
        final_output_keys=list(run_result.data.keys()),
    )

    llm_timeout = float(getattr(getattr(limits, "llm", None), "timeout", 60.0)) if limits else 60.0
    llm_max_retries = int(getattr(getattr(limits, "llm", None), "max_retries", 3)) if limits else 3
    adapted, adapt_usage = await _adapt_artifact(
        run_result.data, sub_skill.final_output_name,
        target_schema, target_type, node_id, output_language,
        model=model, resolver=resolver, events=events,
        llm_timeout=llm_timeout,
        llm_max_retries=llm_max_retries,
    )
    return adapted, token_usage + adapt_usage
