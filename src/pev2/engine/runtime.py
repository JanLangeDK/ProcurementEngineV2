"""A small durable workflow runtime (section 11.2 assumes Temporal or Dapr; this is the mock equivalent).

Durability by replay: a workflow is a plain Python function over a Context. Every step result is persisted
under its step name; when an instance resumes (after a wait, an approval, a review or a restart) the
function runs again from the top and completed steps return their stored results without executing.
Waits raise `Suspend`, which persists the status and stops the run. Business exceptions either follow
the modelled exception path (the workflow catches them) or park the instance for a human. Everything
is written to the audit log with the instance id.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from pev2 import config, store
from pev2.errors import BusinessException, GateFailed, LimitError, TransientError
from pev2.observability import record

WorkflowFn = Callable[["Context"], str]
WORKFLOWS: dict[str, WorkflowFn] = {}
WORKFLOW_META: dict[str, dict[str, Any]] = {}


def workflow(name: str, *, title: str, trigger: str, end_states: list[str]) -> Callable[[WorkflowFn], WorkflowFn]:
    def deco(fn: WorkflowFn) -> WorkflowFn:
        WORKFLOWS[name] = fn
        WORKFLOW_META[name] = {"title": title, "trigger": trigger, "end_states": end_states}
        return fn

    return deco


class Suspend(Exception):
    def __init__(self, status: str, reason: str) -> None:
        super().__init__(reason)
        self.status = status
        self.reason = reason


class Parked(Exception):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass
class Context:
    instance_id: str
    workflow: str
    trigger: dict[str, Any]
    state: dict[str, Any]

    # ---- steps ------------------------------------------------------------------------------------
    def step(self, name: str, fn: Callable[[], Any], *, component: str = "", kind: str = "service") -> Any:
        """Run (or replay) one activity. TransientError after retries parks the instance; BusinessException
        propagates so the workflow can follow its modelled exception path."""
        key = f"step:{name}"
        if key in self.state:
            return self.state[key]
        record(self.instance_id, name, kind, component, "started")
        try:
            result = fn()
        except BusinessException as exc:
            record(self.instance_id, name, "exception", component, exc.kind, {"detail": exc.detail, **exc.data})
            raise
        except TransientError as exc:
            record(self.instance_id, name, kind, component, "retry_exhausted", {"error": str(exc)})
            raise Parked(f"{name}: {exc}. Parked until the integration is back; nothing was assumed.") from exc
        except LimitError as exc:
            record(self.instance_id, name, kind, component, "refused", {"error": str(exc)})
            raise Parked(f"{name}: {exc}") from exc
        self.state[key] = result
        self._save(current=name)
        record(self.instance_id, name, kind, component, "ok", _short(result))
        return result

    def parallel(self, group: str, steps: list[tuple[str, Callable[[], Any], str]]) -> dict[str, Any]:
        """Independent read-only steps; executed one after the other here and recorded as a parallel group."""
        out: dict[str, Any] = {}
        for name, fn, component in steps:
            record(self.instance_id, name, "parallel", component, group)
            out[name] = self.step(name, fn, component=component, kind="tool")
        return out

    def decision(self, name: str, branch: str, condition: str = "") -> str:
        key = f"decision:{name}"
        if key not in self.state:
            self.state[key] = branch
            self._save(current=name)
            record(self.instance_id, name, "decision", "orchestrator", branch, {"condition": condition})
        return str(self.state[key])

    def gate(self, name: str, checks: list[tuple[bool, str]]) -> None:
        """A validation gate: all checks must hold, otherwise the instance goes to human review."""
        key = f"gate:{name}"
        if self.state.get(key) == "passed":
            return
        failed = [reason for ok, reason in checks if not ok]
        if failed:
            record(self.instance_id, name, "gate", name, "failed", {"reasons": failed})
            raise GateFailed(name, failed)
        self.state[key] = "passed"
        self._save(current=name)
        record(self.instance_id, name, "gate", name, "passed")

    # ---- waits -------------------------------------------------------------------------------------
    def wait_event(self, name: str, kind: str, correlation: str, timeout: timedelta | None = None) -> dict[str, Any]:
        key = f"event:{name}"
        if key in self.state:
            return dict(self.state[key])
        row = store.one(
            "select * from wf_event where kind = ? and correlation = ? and consumed_by is null order by id",
            (kind, correlation),
        )
        if row is None:
            due = (config.now() + timeout).isoformat() if timeout else None
            self.state["waiting"] = {"kind": kind, "correlation": correlation, "name": name, "due": due}
            self._save(current=name)
            record(self.instance_id, name, "event", kind, "waiting", {"correlation": correlation, "due": due})
            raise Suspend("waiting_event", f"Wait for: {kind} ({correlation})")
        store.run("update wf_event set consumed_by = ? where id = ?", (self.instance_id, row["id"]))
        payload = store.loads(row["payload"]) or {}
        self.state[key] = payload
        self.state.pop("waiting", None)
        self._save(current=name)
        record(self.instance_id, name, "event", kind, "received", _short(payload))
        return dict(payload)

    def approval(
        self, name: str, owner: str, payload: dict[str, Any], timeout_days: int = 2, escalation: str = ""
    ) -> dict[str, Any]:
        """Pause for a human decision. Returns {'decision': 'approved'|'rejected', 'by', 'note', 'id'}."""
        key = f"approval:{name}"
        if key in self.state:
            return dict(self.state[key])
        row = store.one("select * from wf_approval where instance_id = ? and name = ?", (self.instance_id, name))
        if row is None:
            aid = uuid.uuid4().hex[:12]
            due = (config.now() + timedelta(days=timeout_days)).isoformat()
            store.run(
                "insert into wf_approval (id, instance_id, name, owner, payload, status, requested_at, due_at, escalated_to) values (?, ?, ?, ?, ?, 'pending', ?, ?, ?)",
                (aid, self.instance_id, name, owner, store.dumps(payload), config.now().isoformat(), due, escalation),
            )
            record(self.instance_id, name, "approval", owner, "requested", {"approval_id": aid, "due": due})
            self._save(current=name)
            raise Suspend("waiting_approval", f"{name}: waiting for {owner}")
        if row["status"] == "pending":
            raise Suspend("waiting_approval", f"{name}: waiting for {owner}")
        result = {"id": row["id"], "decision": row["status"], "by": row["decided_by"], "note": row["decision_note"]}
        self.state[key] = result
        self._save(current=name)
        record(self.instance_id, name, "approval", owner, row["status"], {"by": row["decided_by"]})
        return result

    def review(self, name: str, reason: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        """Human review of a parked situation (section 7). Resumes with the reviewer's decision."""
        key = f"review:{name}"
        if key in self.state:
            return dict(self.state[key])
        row = store.one(
            "select * from wf_review where instance_id = ? and step = ? order by id desc", (self.instance_id, name)
        )
        if row is None:
            store.run(
                "insert into wf_review (instance_id, step, reason, payload, status, created_at) values (?, ?, ?, ?, 'open', ?)",
                (self.instance_id, name, reason, store.dumps(payload or {}), config.now().isoformat()),
            )
            record(self.instance_id, name, "review", "exception_handler", "requested", {"reason": reason})
            self._save(current=name)
            raise Suspend("parked", reason)
        if row["status"] == "open":
            raise Suspend("parked", reason)
        result = {"decision": row["status"], "note": store.loads(row["payload"])}
        self.state[key] = result
        self._save(current=name)
        return result

    # ---- helpers -----------------------------------------------------------------------------------
    def idem(self, name: str) -> str:
        return f"{self.instance_id}:{name}"

    def counter(self, name: str) -> int:
        """A per-instance counter for loops (re-asking a supplier), so step names stay unique."""
        key = f"counter:{name}"
        self.state[key] = int(self.state.get(key, 0)) + 1
        self._save()
        return int(self.state[key])

    def _save(self, current: str | None = None) -> None:
        store.run(
            "update wf_instance set state = ?, current_step = coalesce(?, current_step), updated_at = ? where id = ?",
            (store.dumps(self.state), current, config.now().isoformat(), self.instance_id),
        )


def _short(value: Any) -> Any:
    text = store.dumps(value)
    return value if len(text) < 1500 else {"truncated": text[:1500]}


class Runtime:
    """Start, resume and drive workflow instances. One process; a real deployment runs workers."""

    def start(self, name: str, trigger: dict[str, Any], instance_id: str | None = None) -> str:
        if name not in WORKFLOWS:
            raise KeyError(f"unknown workflow {name}")
        iid = instance_id or uuid.uuid4().hex[:12]
        now = config.now().isoformat()
        store.run(
            "insert into wf_instance (id, workflow, status, trigger, state, current_step, created_at, updated_at) values (?, ?, 'running', ?, '{}', 'start', ?, ?)",
            (iid, name, store.dumps(trigger), now, now),
        )
        record(iid, "start", "event", WORKFLOW_META[name]["trigger"], "started", _short(trigger))
        self.run(iid)
        return iid

    def run(self, instance_id: str) -> dict[str, Any]:
        row = store.one("select * from wf_instance where id = ?", (instance_id,))
        if row is None:
            raise KeyError(instance_id)
        if row["status"] in ("completed", "failed"):
            return dict(row)
        ctx = Context(
            instance_id=instance_id,
            workflow=row["workflow"],
            trigger=store.loads(row["trigger"]) or {},
            state=store.loads(row["state"]) or {},
        )
        fn = WORKFLOWS[row["workflow"]]
        status, end_state, reason = "running", None, None
        try:
            end_state = fn(ctx)
            status = "completed"
            record(instance_id, end_state, "end", "orchestrator", "completed")
        except Suspend as s:
            status, reason = s.status, s.reason
        except Parked as p:
            status, reason = "parked", p.reason
            store.run(
                "insert into wf_review (instance_id, step, reason, payload, status, created_at) values (?, ?, ?, '{}', 'open', ?)",
                (instance_id, ctx.state.get("current", "step"), p.reason, config.now().isoformat()),
            )
            record(instance_id, "exception_handler", "exception", "exception_handler", "parked", {"reason": p.reason})
        except GateFailed as g:
            status, reason = "parked", str(g)
            store.run(
                "insert into wf_review (instance_id, step, reason, payload, status, created_at) values (?, ?, ?, ?, 'open', ?)",
                (instance_id, g.gate, str(g), store.dumps({"reasons": g.reasons}), config.now().isoformat()),
            )
            record(instance_id, g.gate, "exception", "exception_handler", "parked", {"reasons": g.reasons})
        except BusinessException as b:
            status, reason = "parked", f"{b.kind}: {b.detail}"
            store.run(
                "insert into wf_review (instance_id, step, reason, payload, status, created_at) values (?, ?, ?, ?, 'open', ?)",
                (instance_id, b.kind, reason, store.dumps(b.data), config.now().isoformat()),
            )
            record(instance_id, "exception_handler", "exception", "exception_handler", "parked", {"reason": reason})
        store.run(
            "update wf_instance set status = ?, end_state = ?, park_reason = ?, state = ?, updated_at = ? where id = ?",
            (status, end_state, reason, store.dumps(ctx.state), config.now().isoformat(), instance_id),
        )
        return dict(store.one("select * from wf_instance where id = ?", (instance_id,)) or {})

    # ---- external signals ------------------------------------------------------------------------------
    def deliver_event(self, kind: str, correlation: str, payload: dict[str, Any]) -> list[str]:
        """An external event (supplier reply, follow-up timer, goods receipt): wake the instances waiting for it."""
        store.run(
            "insert into wf_event (kind, correlation, payload, created_at) values (?, ?, ?, ?)",
            (kind, correlation, store.dumps(payload), config.now().isoformat()),
        )
        woken = []
        for row in store.q("select id, state from wf_instance where status = 'waiting_event'"):
            waiting = (store.loads(row["state"]) or {}).get("waiting") or {}
            if waiting.get("kind") == kind and waiting.get("correlation") == correlation:
                self.run(row["id"])
                woken.append(row["id"])
        return woken

    def decide_approval(self, approval_id: str, decision: str, by: str, note: str = "") -> dict[str, Any]:
        row = store.one("select * from wf_approval where id = ?", (approval_id,))
        if row is None:
            raise KeyError(approval_id)
        if row["status"] != "pending":
            raise ValueError(f"approval {approval_id} is already {row['status']}")
        if decision not in ("approved", "rejected"):
            raise ValueError("decision must be approved or rejected")
        store.run(
            "update wf_approval set status = ?, decided_by = ?, decision_note = ?, decided_at = ? where id = ?",
            (decision, by, note, config.now().isoformat(), approval_id),
        )
        record(row["instance_id"], row["name"], "approval", row["owner"], decision, {"by": by, "note": note})
        return self.run(row["instance_id"])

    def decide_review(self, review_id: int, decision: str, note: str = "", by: str = "") -> dict[str, Any]:
        row = store.one("select * from wf_review where id = ?", (review_id,))
        if row is None:
            raise KeyError(review_id)
        store.run(
            "update wf_review set status = ?, payload = ?, decided_at = ? where id = ?",
            (decision, store.dumps({"note": note, "by": by}), config.now().isoformat(), review_id),
        )
        record(row["instance_id"], row["step"], "review", "exception_handler", decision, {"by": by, "note": note})
        if decision == "abandon":
            store.run(
                "update wf_instance set status = 'failed', end_state = 'Abandoned by reviewer', updated_at = ? where id = ?",
                (config.now().isoformat(), row["instance_id"]),
            )
            return dict(store.one("select * from wf_instance where id = ?", (row["instance_id"],)) or {})
        # a parked step is retried: drop nothing (completed steps replay), just run again
        store.run("update wf_instance set status = 'running' where id = ?", (row["instance_id"],))
        return self.run(row["instance_id"])

    def tick(self) -> list[dict[str, Any]]:
        """The scheduler: escalate overdue approvals, time out overdue waits (delivering a timeout event)."""
        actions: list[dict[str, Any]] = []
        now = config.now().isoformat()
        for a in store.q(
            "select * from wf_approval where status = 'pending' and due_at < ? and (escalated_to is null or escalated_to = '' or owner not like '%(escalated)%')",
            (now,),
        ):
            target = a["escalated_to"] or a["owner"]
            store.run(
                "update wf_approval set owner = ?, due_at = ? where id = ?",
                (f"{target} (escalated)", (config.now() + timedelta(days=2)).isoformat(), a["id"]),
            )
            record(a["instance_id"], a["name"], "approval", a["owner"], "escalated", {"to": target})
            actions.append({"approval": a["id"], "escalated_to": target})
        for row in store.q("select id, state from wf_instance where status = 'waiting_event'"):
            waiting = (store.loads(row["state"]) or {}).get("waiting") or {}
            if waiting.get("due") and waiting["due"] < now:
                self.deliver_event(waiting["kind"], waiting["correlation"], {"timed_out": True})
                actions.append({"instance": row["id"], "timed_out": waiting["kind"]})
        return actions

    # ---- queries ----------------------------------------------------------------------------------------
    def instances(self, limit: int = 100) -> list[dict[str, Any]]:
        return store.rows(
            "select id, workflow, status, current_step, end_state, park_reason, created_at, updated_at from wf_instance order by created_at desc limit ?",
            (limit,),
        )

    def instance(self, instance_id: str) -> dict[str, Any] | None:
        row = store.one("select * from wf_instance where id = ?", (instance_id,))
        if row is None:
            return None
        d = dict(row)
        d["state"] = store.loads(d["state"])
        d["trigger"] = store.loads(d["trigger"])
        return d

    def approvals(self, status: str = "pending") -> list[dict[str, Any]]:
        rows = (
            store.rows("select * from wf_approval where status = ? order by requested_at", (status,))
            if status != "all"
            else store.rows("select * from wf_approval order by requested_at desc")
        )
        for r in rows:
            r["payload"] = store.loads(r["payload"])
        return rows

    def reviews(self, status: str = "open") -> list[dict[str, Any]]:
        rows = (
            store.rows("select * from wf_review where status = ? order by id desc", (status,))
            if status != "all"
            else store.rows("select * from wf_review order by id desc")
        )
        for r in rows:
            r["payload"] = store.loads(r["payload"])
        return rows


runtime = Runtime()
