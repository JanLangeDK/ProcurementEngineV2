"""`pev2 demo`: reset and play the four stories unattended against the mocks; `pev2 smoke --engine`: phase-0 gate."""

from __future__ import annotations

import os

from pev2 import store
from pev2.app import ready, runtime
from pev2.tools import email, sap


def say(text: str) -> None:
    print(text, flush=True)


def _state(iid: str) -> str:
    i = runtime.instance(iid) or {}
    return f"{i.get('status')} → {i.get('end_state') or i.get('current_step')}"


def play() -> bool:
    os.environ.setdefault("PEV2_CLOCK", "2026-10-20T08:00:00Z")
    store.init()
    store.reset()
    ready()
    ok = True
    say("A · Production purchase: 2000 rolls M2001 for Horsens")
    a = runtime.start(
        "production_purchase",
        {
            "material_id": "M2001",
            "quantity": 2000,
            "required_date": "2026-10-30",
            "requester": "Lars Bak",
            "plant": "Horsens",
        },
    )
    for ap in runtime.approvals():
        runtime.decide_approval(ap["id"], "approved", "Procurement Manager (demo)")
    po = next(
        (
            v["purchase_order"]["po_number"]
            for k, v in (runtime.instance(a) or {})["state"].items()
            if k.startswith("step:Submit purchase order")
        ),
        None,
    )
    if po:
        sap.post_confirmation(po, "confirmed", "2026-10-29")
        runtime.deliver_event("supplier_confirmation", po, {"status": "confirmed"})
    say(f"   {_state(a)} (PO {po})")
    ok &= (runtime.instance(a) or {}).get("end_state") == "Requirement fulfilled"

    say("B · Invoice with a price deviation: INV7731")
    b = runtime.start("price_deviation", {"invoice_id": "INV7731", "sender": "ap@krydderihuset.example"})
    for ap in runtime.approvals():
        runtime.decide_approval(ap["id"], "approved", "AP Specialist (demo)")
    email.inbound(
        "ap@krydderihuset.example",
        "Re: invoice",
        "Sorry, our mistake, credit note CN-2026-118 follows for the difference.",
        thread_id="inv:INV7731",
    )
    runtime.deliver_event(
        "supplier_reply",
        "inv:INV7731",
        {
            "body": "Sorry, our mistake, credit note CN-2026-118 follows for the difference.",
            "from_addr": "ap@krydderihuset.example",
        },
    )
    for ap in runtime.approvals():
        runtime.decide_approval(ap["id"], "approved", "AP Team Lead (demo)")
    say(
        f"   {_state(b)}; invoice {dict(store.one('select status, credit_note_ref from sap_invoice where id=?', ('INV7731',)) or {})}"
    )
    ok &= (runtime.instance(b) or {}).get("end_state") == "Invoice released for payment"

    say("C · A supplier asks when it gets paid: 2026-0912")
    c = runtime.start(
        "supplier_query", {"message": "When will invoice 2026-0912 be paid?", "sender": "ap@danfilm-plast.example"}
    )
    answer = store.one(
        "select body from mail_message where direction='out' and status='sent' and subject like 'Your payment%' order by id desc"
    )
    say(f"   {_state(c)}; answer: {answer['body'] if answer else None}")
    ok &= (runtime.instance(c) or {}).get("end_state") == "Enquiry answered"

    say("D · Invoice processing (general): INV7701 clean, INV7732 with discrepancies")
    d1 = runtime.start("invoice_processing", {"invoice_id": "INV7701", "sender": "edi"})
    d2 = runtime.start("invoice_processing", {"invoice_id": "INV7732", "sender": "edi"})
    for ap in runtime.approvals():
        runtime.decide_approval(ap["id"], "approved", "AP Specialist (demo)")
    runtime.deliver_event(
        "supplier_reply",
        "inv:INV7732",
        {
            "body": "Sorry, corrected invoice follows, credit note CN-2026-120 for the difference.",
            "from_addr": "ap@jysk-rengoeringskemi.example",
        },
    )
    for ap in runtime.approvals():
        runtime.decide_approval(ap["id"], "approved", "AP Team Lead (demo)")
    say(f"   INV7701: {_state(d1)}\n   INV7732: {_state(d2)}")
    ok &= (runtime.instance(d1) or {}).get("end_state") == "Processing completed"
    say("PASS" if ok else "FAIL")
    return ok


def smoke() -> bool:
    """Phase 0 gate: a workflow runs, persists its state, survives a 'restart' (a new runtime over the same file)."""
    os.environ.setdefault("PEV2_CLOCK", "2026-10-20T08:00:00Z")
    store.init()
    store.reset()
    ready()
    iid = runtime.start(
        "supplier_query", {"message": "When will invoice 2026-0912 be paid?", "sender": "ap@danfilm-plast.example"}
    )
    before = runtime.instance(iid)
    from pev2.engine.runtime import Runtime

    fresh = Runtime()
    after = fresh.instance(iid)
    audit = store.one("select count(*) as n from wf_audit where instance_id = ?", (iid,))
    ok = bool(
        before
        and after
        and after["status"] == "completed"
        and after["end_state"] == "Enquiry answered"
        and audit
        and audit["n"] > 5
    )
    print(
        f"smoke: instance {iid} {after['status'] if after else '?'} after restart, {audit['n'] if audit else 0} audit rows → {'PASS' if ok else 'FAIL'}"
    )
    return ok
