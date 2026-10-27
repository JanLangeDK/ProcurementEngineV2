"""The HTTP side of the backend: events in, approvals and reviews decided, instances and audit read.
The Gradio window uses the runtime in-process; this API is for other clients and for tests."""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel

from pev2 import store
from pev2.app import ready, runtime
from pev2.engine.runtime import WORKFLOW_META
from pev2.investigation import discoveries, gap_report, investigate_instance, set_discovery_status
from pev2.observability import feed
from pev2.tools import email, sap

app = FastAPI(title="Procurement Engine V2", version="0.1.0")


class StartBody(BaseModel):
    workflow: str
    trigger: dict[str, Any]


class EventBody(BaseModel):
    kind: str
    correlation: str
    payload: dict[str, Any] = {}


class DecisionBody(BaseModel):
    decision: str
    by: str
    note: str = ""


@app.on_event("startup")
def _startup() -> None:
    ready()


@app.get("/api/health")
def health() -> dict[str, Any]:
    return {"status": "ok", "workflows": WORKFLOW_META}


@app.post("/api/instances", status_code=201)
def start(body: StartBody) -> dict[str, Any]:
    try:
        iid = runtime.start(body.workflow, body.trigger)
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc
    return runtime.instance(iid) or {}


@app.get("/api/instances")
def instances() -> list[dict[str, Any]]:
    return runtime.instances()


@app.get("/api/instances/{iid}")
def instance(iid: str) -> dict[str, Any]:
    row = runtime.instance(iid)
    if row is None:
        raise HTTPException(404, "instance not found")
    return {**row, "audit": list(reversed(feed(iid, 500)))}


@app.post("/api/events")
def event(body: EventBody) -> dict[str, Any]:
    if body.kind == "supplier_reply":
        email.inbound(
            body.payload.get("from_addr", ""),
            f"Re: {body.correlation}",
            body.payload.get("body", ""),
            thread_id=body.correlation,
        )
    if body.kind == "supplier_confirmation":
        sap.post_confirmation(
            body.correlation,
            body.payload.get("status", "confirmed"),
            body.payload.get("confirmed_date"),
            body.payload.get("note", ""),
        )
    return {"woken": runtime.deliver_event(body.kind, body.correlation, body.payload)}


@app.get("/api/approvals")
def approvals(status: str = "pending") -> list[dict[str, Any]]:
    return runtime.approvals(status)


@app.post("/api/approvals/{aid}")
def decide(aid: str, body: DecisionBody) -> dict[str, Any]:
    try:
        return runtime.decide_approval(aid, body.decision, body.by, body.note)
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc


@app.get("/api/reviews")
def reviews(status: str = "open") -> list[dict[str, Any]]:
    return runtime.reviews(status)


@app.post("/api/reviews/{rid}")
def decide_review(rid: int, body: DecisionBody) -> dict[str, Any]:
    try:
        return runtime.decide_review(rid, body.decision, body.note, body.by)
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc


@app.post("/api/tick")
def tick() -> list[dict[str, Any]]:
    return runtime.tick()


@app.get("/api/audit")
def audit(instance_id: str | None = None, limit: int = 200) -> list[dict[str, Any]]:
    return feed(instance_id, limit)


@app.post("/api/reset")
def reset() -> dict[str, str]:
    store.reset()
    return {"status": "reset"}


@app.get("/api/sap/invoices/{invoice_id}")
def sap_invoice(invoice_id: str) -> dict[str, Any]:
    row = store.one("select * from sap_invoice where id = ?", (invoice_id,))
    if row is None:
        raise HTTPException(404, "invoice not found")
    return dict(row)


@app.post("/api/instances/{iid}/investigate")
def investigate(iid: str) -> dict[str, Any]:
    """Side investigation of a parked instance: a read-only proposal plus recorded discoveries; the instance is untouched."""
    try:
        return investigate_instance(iid)
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc


@app.get("/api/discoveries")
def list_discoveries(status: str = "all") -> list[dict[str, Any]]:
    return discoveries(status)


@app.post("/api/discoveries/{fingerprint}")
def mark_discovery(fingerprint: str, body: DecisionBody) -> dict[str, str]:
    set_discovery_status(fingerprint, body.decision)
    return {"status": body.decision}


@app.get("/api/discoveries/report", response_class=PlainTextResponse)
def discovery_report() -> str:
    return gap_report()
