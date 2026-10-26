"""Structured events with the process instance id: every step, tool call, model call and approval."""

from __future__ import annotations

import logging
from typing import Any

from pev2 import config, store

log = logging.getLogger("pev2")


def record(
    instance_id: str | None, step: str, kind: str, component: str = "", status: str = "ok", detail: Any = None
) -> None:
    """Append one audit row. `kind` is one of step | tool | model | approval | gate | event | exception | review."""
    store.run(
        "insert into wf_audit (instance_id, step, kind, component, status, detail, at) values (?, ?, ?, ?, ?, ?, ?)",
        (
            instance_id,
            step,
            kind,
            component,
            status,
            store.dumps(detail) if detail is not None else None,
            config.now().isoformat(),
        ),
    )
    log.info("%s %s %s %s %s", instance_id or "-", kind, step, component, status)


def feed(instance_id: str | None = None, limit: int = 200) -> list[dict[str, Any]]:
    if instance_id:
        return store.rows("select * from wf_audit where instance_id = ? order by id desc limit ?", (instance_id, limit))
    return store.rows("select * from wf_audit order by id desc limit ?", (limit,))
