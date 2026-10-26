"""The model gateway: the only place an agent runs. Fixture provider (deterministic) or an OpenAI-compatible
endpoint (Azure AI Foundry included). Output is validated against the agent's schema; schema-invalid output is
retried once with the error, then routed to human review (GateFailed). Low confidence (< 0.6) also routes to review.
"""

from __future__ import annotations

import json
from typing import Any

from pydantic import ValidationError

from pev2 import config
from pev2.agents import AGENTS, AgentSpec
from pev2.errors import GateFailed
from pev2.observability import record
from pev2.services.common import Tools
from pev2.tools import registry

CONFIDENCE_FLOOR = 0.6


def run_agent(agent_id: str, inputs: dict[str, Any], instance_id: str | None, objective: str = "") -> dict[str, Any]:
    spec = AGENTS[agent_id]
    tools = Tools(caller=spec.id, role="agent", instance_id=instance_id)
    provider = config.llm_provider()
    record(
        instance_id, spec.name, "model", provider, "started", {"objective": objective, "inputs": list(inputs.keys())}
    )
    raw = (
        _fixture(spec, inputs, tools) if provider == "fixture" else _model(spec, inputs, tools, objective, instance_id)
    )
    try:
        out = spec.output.model_validate(raw)
    except ValidationError as exc:
        record(instance_id, spec.name, "model", provider, "schema_invalid", {"error": str(exc)[:500]})
        if provider == "fixture":
            raise GateFailed(f"{spec.name} output", [f"schema-invalid output: {exc}"]) from exc
        raw = _model(spec, inputs, tools, objective, instance_id, retry_error=str(exc)[:800])
        try:
            out = spec.output.model_validate(raw)
        except ValidationError as exc2:
            raise GateFailed(f"{spec.name} output", [f"schema-invalid output after retry: {exc2}"]) from exc2
    result = out.model_dump()
    if float(result.get("confidence", 0)) < CONFIDENCE_FLOOR:
        record(instance_id, spec.name, "model", provider, "low_confidence", {"confidence": result.get("confidence")})
        raise GateFailed(
            f"{spec.name} confidence",
            [f"confidence {result.get('confidence')} is below {CONFIDENCE_FLOOR}; human review"],
        )
    record(
        instance_id,
        spec.name,
        "model",
        provider,
        "ok",
        {"confidence": result.get("confidence"), "rationale": result.get("rationale")},
    )
    return result


def _fixture(spec: AgentSpec, inputs: dict[str, Any], tools: Tools) -> dict[str, Any]:
    return spec.fixture(inputs, tools)


def _model(
    spec: AgentSpec,
    inputs: dict[str, Any],
    tools: Tools,
    objective: str,
    instance_id: str | None,
    retry_error: str | None = None,
) -> dict[str, Any]:
    from openai import OpenAI

    s = config.llm_settings()
    if not s["api_key"] or not s["base_url"]:
        raise GateFailed(spec.name, ["no model endpoint configured (PEV2_LLM_BASE_URL / PEV2_LLM_API_KEY)"])
    headers = {"api-key": s["api_key"]} if "azure" in str(s["base_url"]).lower() else {}
    client = OpenAI(base_url=s["base_url"], api_key=s["api_key"], default_headers=headers, timeout=spec.timeout_seconds)
    schema = spec.output.model_json_schema()
    tool_defs = [
        {
            "type": "function",
            "function": {
                "name": tid.replace(".", "__"),
                "description": registry.specs[tid].description or registry.specs[tid].name,
                "parameters": {"type": "object", "properties": {"args": {"type": "object"}}, "required": ["args"]},
            },
        }
        for tid in spec.tools
    ]
    user = (
        "<data>\n"
        + json.dumps(inputs, ensure_ascii=False, default=str)[:20000]
        + "\n</data>\n\nObjective: "
        + (objective or spec.purpose)
    )
    if retry_error:
        user += f"\n\nYour previous answer was rejected: {retry_error}. Return valid JSON for the schema."
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": spec.instructions + "\n\nOutput JSON schema:\n" + json.dumps(schema)},
        {"role": "user", "content": user},
    ]
    kwargs: dict[str, Any] = {"model": s["model"], "messages": messages, "response_format": {"type": "json_object"}}
    if s["temperature"]:
        kwargs["temperature"] = float(s["temperature"])
    if tool_defs:
        kwargs["tools"] = tool_defs
    calls = 0
    for _ in range(spec.max_iterations):
        resp = client.chat.completions.create(**kwargs)
        msg = resp.choices[0].message
        if msg.tool_calls:
            messages.append(
                {
                    "role": "assistant",
                    "content": msg.content or "",
                    "tool_calls": [tc.model_dump() for tc in msg.tool_calls],
                }
            )
            for tc in msg.tool_calls:
                calls += 1
                if calls > spec.max_tool_calls:
                    raise GateFailed(spec.name, ["tool call budget exhausted"])
                tid = tc.function.name.replace("__", ".")
                try:
                    args = json.loads(tc.function.arguments or "{}").get("args", {})
                    out = tools(
                        tid,
                        idempotency_key=f"{instance_id}:{spec.id}:{calls}"
                        if registry.specs[tid].side_effect != "read"
                        else None,
                        **args,
                    )
                except Exception as exc:  # noqa: BLE001 - the model sees the refusal as data
                    out = {"error": str(exc)}
                messages.append({"role": "tool", "tool_call_id": tc.id, "content": json.dumps(out, default=str)[:8000]})
            kwargs["messages"] = messages
            continue
        text = msg.content or "{}"
        try:
            return dict(json.loads(text))
        except json.JSONDecodeError:
            start, end = text.find("{"), text.rfind("}")
            return dict(json.loads(text[start : end + 1])) if start >= 0 else {}
    raise GateFailed(spec.name, ["iteration budget exhausted"])
