"""Catalog tools: one registry, schemas and side-effect classes from section 3 of docs/architecture.md.

Every call goes through `call()`: the caller's allow-list is checked (a component may only use the tools
its specification lists), write tools need an idempotency key (a repeated key returns the stored result
without executing again), transient errors are retried per the tool's policy, and every call is audited.
Hard limits (the authority matrix) live in `limits.py` and are enforced here, never in a prompt.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from pev2 import config, store
from pev2.errors import LimitError, TransientError
from pev2.observability import record
from pev2.tools import limits

SideEffect = str  # read | write | notify


@dataclass(frozen=True)
class ToolSpec:
    id: str
    name: str
    integration: str
    side_effect: SideEffect = "read"
    idempotent: bool = True
    permissions: tuple[str, ...] = ()
    timeout_seconds: int = 30
    max_attempts: int = 3
    description: str = ""


ToolFn = Callable[[dict[str, Any]], dict[str, Any]]


@dataclass
class Registry:
    specs: dict[str, ToolSpec] = field(default_factory=dict)
    fns: dict[str, ToolFn] = field(default_factory=dict)
    allow: dict[str, frozenset[str]] = field(default_factory=dict)  # caller component id -> tool ids
    outages: set[str] = field(default_factory=set)  # integrations simulated as unavailable

    def register(self, spec: ToolSpec, fn: ToolFn) -> None:
        self.specs[spec.id] = spec
        self.fns[spec.id] = fn

    def permit(self, caller: str, tool_ids: list[str] | tuple[str, ...]) -> None:
        self.allow[caller] = frozenset(tool_ids)

    def allowed(self, caller: str) -> frozenset[str]:
        return self.allow.get(caller, frozenset())

    def call(
        self,
        tool_id: str,
        args: dict[str, Any],
        *,
        caller: str,
        instance_id: str | None = None,
        idempotency_key: str | None = None,
        role: str | None = None,
        approval_ref: str | None = None,
    ) -> dict[str, Any]:
        spec = self.specs.get(tool_id)
        if spec is None:
            raise LimitError(f"{tool_id} is not a tool this system offers.")
        if tool_id not in self.allowed(caller):
            record(instance_id, caller, "tool", tool_id, "refused", {"reason": "not in allow-list"})
            raise LimitError(f"{caller} may not call {tool_id}: it is not in its tool allow-list.")
        if spec.side_effect != "read":
            if not idempotency_key:
                raise LimitError(f"{tool_id} is a {spec.side_effect} operation and needs an idempotency key.")
            cached = store.one("select result from wf_idempotency where key = ?", (idempotency_key,))
            if cached is not None:
                record(instance_id, caller, "tool", tool_id, "replayed", {"idempotency_key": idempotency_key})
                return dict(store.loads(cached["result"]))
            limits.check_write(tool_id, args, role=role, approval_ref=approval_ref)
        if spec.integration in self.outages:
            # the outage is simulated for every attempt: the retry budget runs out and the instance parks
            for attempt in range(1, spec.max_attempts + 1):
                record(
                    instance_id,
                    caller,
                    "tool",
                    tool_id,
                    "transient_error",
                    {"attempt": attempt, "integration": spec.integration},
                )
                if config.retry_sleep():
                    time.sleep(config.retry_sleep() * attempt)
            raise TransientError(f"{spec.integration} is unavailable (retry budget of {spec.max_attempts} exhausted)")
        last: Exception | None = None
        for attempt in range(1, spec.max_attempts + 1):
            try:
                result = self.fns[tool_id](dict(args))
                break
            except TransientError as exc:
                last = exc
                record(instance_id, caller, "tool", tool_id, "transient_error", {"attempt": attempt, "error": str(exc)})
                if config.retry_sleep():
                    time.sleep(config.retry_sleep() * attempt)
        else:
            raise TransientError(str(last))
        if spec.side_effect != "read" and idempotency_key:
            store.run(
                "insert or replace into wf_idempotency (key, tool, result, created_at) values (?, ?, ?, ?)",
                (idempotency_key, tool_id, store.dumps(result), config.now().isoformat()),
            )
        record(
            instance_id,
            caller,
            "tool",
            tool_id,
            "ok",
            {"args": _redact(args), "side_effect": spec.side_effect, "idempotency_key": idempotency_key},
        )
        return result


def _redact(args: dict[str, Any]) -> dict[str, Any]:
    return {k: (v if len(str(v)) < 300 else str(v)[:300] + "…") for k, v in args.items()}


registry = Registry()
