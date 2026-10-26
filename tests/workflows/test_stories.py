"""End-to-end runs of the four workflows against the mocks, approvals driven through the runtime API."""

from __future__ import annotations

from conftest import approve_all

from pev2 import store
from pev2.tools import email, sap


def _po_of(rt, iid):
    return next(
        (
            v["purchase_order"]["po_number"]
            for k, v in rt.instance(iid)["state"].items()
            if k.startswith("step:Submit purchase order")
        ),
        None,
    )


def test_price_deviation_story_b(rt):
    iid = rt.start("price_deviation", {"invoice_id": "INV7731", "sender": "ap@krydderihuset.example"})
    inst = rt.instance(iid)
    assert inst["status"] == "waiting_approval" and inst["current_step"] == "Approve outbound supplier communication"
    assert store.one("select status, block_reason from sap_invoice where id='INV7731'")["status"] == "blocked"
    assert store.one("select count(*) as n from mail_message where status='sent'")["n"] == 0, (
        "nothing is sent before the approval"
    )
    assert approve_all(rt, "Sofie") == 1
    assert rt.instance(iid)["status"] == "waiting_event"
    assert store.one("select count(*) as n from mail_message where status='sent'")["n"] == 1
    email.inbound(
        "ap@krydderihuset.example",
        "Re",
        "Sorry, our mistake, credit note CN-2026-118 follows for the difference.",
        thread_id="inv:INV7731",
    )
    rt.deliver_event(
        "supplier_reply",
        "inv:INV7731",
        {
            "body": "Sorry, our mistake, credit note CN-2026-118 follows for the difference.",
            "from_addr": "ap@krydderihuset.example",
        },
    )
    inst = rt.instance(iid)
    assert inst["status"] == "waiting_approval" and inst["current_step"] == "Release invoice"
    assert store.one("select status from sap_invoice where id='INV7731'")["status"] == "parked"
    release = rt.approvals()[0]
    assert release["owner"] == "AP Team Lead"
    rt.decide_approval(release["id"], "approved", "Mette")
    inst = rt.instance(iid)
    assert inst["status"] == "completed" and inst["end_state"] == "Invoice released for payment"
    row = store.one("select status, credit_note_ref, released_by from sap_invoice where id='INV7731'")
    assert row["status"] == "posted" and row["credit_note_ref"] == "CN-2026-118" and row["released_by"] == "Mette"
    # the gated write never ran before the decision: the release tool call is audited after the approval
    kinds = [
        (a["kind"], a["component"], a["status"])
        for a in store.rows("select kind, component, status from wf_audit where instance_id=? order by id", (iid,))
    ]
    assert ("tool", "sap.release_invoice", "ok") in kinds
    assert kinds.index(("approval", "AP Team Lead", "approved")) < kinds.index(("tool", "sap.release_invoice", "ok"))


def test_price_deviation_supplier_disputes_escalates(rt):
    iid = rt.start("price_deviation", {"invoice_id": "INV7731", "sender": "ap@krydderihuset.example"})
    approve_all(rt)
    rt.deliver_event(
        "supplier_reply",
        "inv:INV7731",
        {
            "body": "52.00 is our current price and we will not issue a credit note.",
            "from_addr": "ap@krydderihuset.example",
        },
    )
    inst = rt.instance(iid)
    assert inst["end_state"] == "Escalated to AP Team Lead"
    assert store.one("select status from sap_invoice where id='INV7731'")["status"] == "blocked", (
        "never parked without a confirmed credit note"
    )


def test_price_deviation_rejected_communication_escalates(rt):
    iid = rt.start("price_deviation", {"invoice_id": "INV7731"})
    rt.decide_approval(rt.approvals()[0]["id"], "rejected", "Sofie", "wrong tone")
    assert rt.instance(iid)["end_state"] == "Escalated to AP Team Lead"
    assert store.one("select count(*) as n from mail_message where status='sent'")["n"] == 0


def test_price_deviation_within_tolerance_parks_directly(rt):
    store.run("update sap_invoice_line set price = '48.20' where invoice_id = 'INV7731'")
    store.run("update sap_invoice set amount = '4820.00' where id = 'INV7731'")
    iid = rt.start("price_deviation", {"invoice_id": "INV7731"})
    inst = rt.instance(iid)
    assert (
        inst["current_step"] == "Release invoice"
        and store.one("select status from sap_invoice where id='INV7731'")["status"] == "parked"
    )
    assert store.one("select count(*) as n from mail_message")["n"] == 0


def test_price_deviation_policy_change_changes_behaviour(rt):
    from pev2.tools import brain

    path = brain.DOCS / "policies" / "invoice-handling.md"
    original = path.read_text()
    try:
        path.write_text(original.replace("up to 1% of the contract line value", "up to 10% of the contract line value"))
        iid = rt.start("price_deviation", {"invoice_id": "INV7731"})
        assert (
            rt.instance(iid)["current_step"] == "Release invoice"
        )  # same code, same invoice, parked with no supplier contact
    finally:
        path.write_text(original)


def test_price_deviation_exceptions_park_for_review(rt):
    store.run("delete from sap_goods_receipt where po_id = 'PO45088'")
    iid = rt.start("price_deviation", {"invoice_id": "INV7731"})
    inst = rt.instance(iid)
    assert inst["status"] == "parked" and "goods_receipt_missing" in inst["park_reason"]
    assert rt.reviews()[0]["instance_id"] == iid


def test_sap_outage_parks_and_resumes_after_review(rt):
    from pev2.tools import registry

    registry.outages = {"sap"}
    iid = rt.start("price_deviation", {"invoice_id": "INV7731"})
    assert rt.instance(iid)["status"] == "parked" and "unavailable" in rt.instance(iid)["park_reason"]
    registry.outages = set()
    rt.decide_review(rt.reviews()[0]["id"], "retry", "SAP is back")
    assert rt.instance(iid)["status"] == "waiting_approval"


def test_supplier_query_story_c_read_only(rt):
    iid = rt.start(
        "supplier_query", {"message": "When will invoice 2026-0912 be paid?", "sender": "ap@danfilm-plast.example"}
    )
    inst = rt.instance(iid)
    assert inst["end_state"] == "Enquiry answered"
    sent = store.rows(
        "select body from mail_message where direction='out' and status='sent' and subject like 'Your payment%'"
    )
    assert (
        len(sent) == 1
        and "That is an expectation, not a promise" in sent[0]["body"]
        and "Thursday 5 November" in sent[0]["body"]
    )
    writes = store.rows(
        "select component from wf_audit where instance_id=? and kind='tool' and component like 'sap.%' and detail like '%\"side_effect\": \"write\"%'",
        (iid,),
    )
    assert writes == [], "nothing is written in SAP"
    assert store.one("select status from case_issue order by created_at desc")["status"] == "answered"


def test_supplier_query_hands_off_unknown_sender_and_unknown_invoice(rt):
    a = rt.start("supplier_query", {"message": "When will invoice 2026-0912 be paid?", "sender": "nobody@example.com"})
    b = rt.start(
        "supplier_query", {"message": "When will invoice 123456 be paid?", "sender": "ap@danfilm-plast.example"}
    )
    assert (
        rt.instance(a)["end_state"] == "Handed to AP Specialist"
        and rt.instance(b)["end_state"] == "Handed to AP Specialist"
    )
    assert store.one("select count(*) as n from mail_message where subject like 'Your payment%'")["n"] == 0, (
        "no invoice information to an unverified contact"
    )
    assert store.one("select assignee from case_issue order by created_at desc")["assignee"] == "AP Specialist"


def test_supplier_query_blocked_invoice_hands_off(rt):
    store.run("update sap_invoice set status = 'blocked' where id = 'INV7702'")
    iid = rt.start(
        "supplier_query", {"message": "When will invoice 2026-0912 be paid?", "sender": "ap@danfilm-plast.example"}
    )
    assert rt.instance(iid)["end_state"] == "Handed to AP Specialist"


def test_production_purchase_story_a(rt):
    iid = rt.start(
        "production_purchase",
        {
            "material_id": "M2001",
            "quantity": 2000,
            "required_date": "2026-10-30",
            "requester": "Lars Bak",
            "plant": "Horsens",
        },
    )
    inst = rt.instance(iid)
    assert inst["status"] == "waiting_event", inst
    po = _po_of(rt, iid)
    assert (
        po
        and store.one("select supplier_id, status from sap_purchase_order where id=?", (po,))["supplier_id"] == "S1001"
    )
    assert rt.approvals() == []  # 77,000 DKK ≈ 10,335 EUR: within the buyer limit
    sap.post_confirmation(po, "confirmed", "2026-10-29")
    rt.deliver_event("supplier_confirmation", po, {"status": "confirmed"})
    assert rt.instance(iid)["end_state"] == "Requirement fulfilled"
    assert (
        store.one("select count(*) as n from mail_message where status='sent' and subject like 'Your requirement%'")[
            "n"
        ]
        == 1
    )


def test_production_purchase_above_limit_needs_procurement_manager(rt):
    iid = rt.start(
        "production_purchase",
        {
            "material_id": "M2201",
            "quantity": 900,
            "required_date": "2026-11-20",
            "requester": "Anette Krogh",
            "plant": "Ringsted",
        },
    )
    inst = rt.instance(iid)
    assert inst["status"] == "waiting_approval"
    ap = rt.approvals()[0]
    assert ap["owner"] == "Procurement Manager" and _po_of(rt, iid) is None, "no PO before the decision"
    rt.decide_approval(ap["id"], "rejected", "Henrik")
    assert rt.instance(iid)["end_state"] == "Requirement cannot be fulfilled" and _po_of(rt, iid) is None


def test_production_purchase_covered_and_unknown_material(rt):
    a = rt.start(
        "production_purchase",
        {
            "material_id": "M2007",
            "quantity": 10,
            "required_date": "2026-11-30",
            "requester": "Anette Krogh",
            "plant": "Ringsted",
        },
    )
    assert (
        rt.instance(a)["end_state"] == "Requirement fulfilled"
        and store.one("select count(*) as n from sap_purchase_order where created_by='workflow'")["n"] == 0
    )
    b = rt.start(
        "production_purchase",
        {"material_id": "M9999", "quantity": 10, "required_date": "2026-11-30", "requester": "x", "plant": "Ringsted"},
    )
    assert rt.instance(b)["status"] == "parked" and "unknown_material" in rt.instance(b)["park_reason"]


def test_production_purchase_rejection_tries_an_alternative_supplier(rt):
    iid = rt.start(
        "production_purchase",
        {
            "material_id": "M2001",
            "quantity": 2000,
            "required_date": "2026-10-30",
            "requester": "Lars Bak",
            "plant": "Horsens",
        },
    )
    po = _po_of(rt, iid)
    assert store.one("select supplier_id from sap_purchase_order where id=?", (po,))["supplier_id"] == "S1001", (
        "cheapest contract price first"
    )
    sap.post_confirmation(po, "rejected", None, "out of stock")
    rt.deliver_event("supplier_confirmation", po, {"status": "rejected"})
    inst = rt.instance(iid)
    assert inst["status"] == "waiting_event" and inst["state"]["excluded_suppliers"] == ["S1001"]
    po2 = inst["state"]["step:Submit purchase order through SAP #1"]["purchase_order"]["po_number"]
    assert (
        po2 != po
        and store.one("select supplier_id from sap_purchase_order where id=?", (po2,))["supplier_id"] == "S1006"
    )
    sap.post_confirmation(po2, "confirmed", "2026-10-29")
    rt.deliver_event("supplier_confirmation", po2, {"status": "confirmed"})
    assert rt.instance(iid)["end_state"] == "Requirement fulfilled"


def test_production_purchase_delay_within_required_date_is_accepted(rt):
    iid = rt.start(
        "production_purchase",
        {
            "material_id": "M2001",
            "quantity": 2000,
            "required_date": "2026-10-30",
            "requester": "Lars Bak",
            "plant": "Horsens",
        },
    )
    po = _po_of(rt, iid)
    sap.post_confirmation(po, "delayed", "2026-10-29", "one day later than asked")
    rt.deliver_event("supplier_confirmation", po, {"status": "delayed", "confirmed_date": "2026-10-29"})
    assert rt.instance(iid)["end_state"] == "Requirement fulfilled"


def test_production_purchase_delay_past_required_date_parks_when_no_alternative(rt):
    store.run("update sap_supplier set blocked = 1 where id = 'S1006'")
    iid = rt.start(
        "production_purchase",
        {
            "material_id": "M2001",
            "quantity": 2000,
            "required_date": "2026-10-30",
            "requester": "Lars Bak",
            "plant": "Horsens",
        },
    )
    po = _po_of(rt, iid)
    sap.post_confirmation(po, "delayed", "2026-11-05")
    rt.deliver_event("supplier_confirmation", po, {"status": "delayed", "confirmed_date": "2026-11-05"})
    inst = rt.instance(iid)
    assert inst["status"] == "parked" and rt.reviews()[0]["instance_id"] == iid
    rt.decide_review(rt.reviews()[0]["id"], "abandon", "production reschedules")
    assert rt.instance(iid)["status"] == "failed" and rt.instance(iid)["end_state"] == "Abandoned by reviewer"


def test_invoice_processing_clean_and_discrepant(rt):
    clean = rt.start("invoice_processing", {"invoice_id": "INV7701", "sender": "edi"})
    assert rt.instance(clean)["end_state"] == "Processing completed"
    assert store.one("select status from sap_invoice where id='INV7701'")["status"] == "posted"
    disc = rt.start("invoice_processing", {"invoice_id": "INV7732", "sender": "edi"})
    inst = rt.instance(disc)
    assert inst["status"] == "waiting_approval" and inst["current_step"] == "Approve outbound supplier communication"
    assert store.one("select status from sap_invoice where id='INV7732'")["status"] == "received", (
        "not posted before resolution"
    )
    approve_all(rt, "Sofie")
    assert rt.instance(disc)["status"] == "waiting_event"
    rt.deliver_event(
        "supplier_reply",
        "inv:INV7732",
        {"body": "Sorry, our mistake, credit note CN-2026-120 follows.", "from_addr": "x"},
    )
    inst = rt.instance(disc)
    assert inst["status"] == "waiting_approval" and inst["current_step"] == "Approve discrepancy"
    approve_all(rt, "Mette")
    assert rt.instance(disc)["end_state"] == "Processing completed"
    assert store.one("select status from sap_invoice where id='INV7732'")["status"] == "posted"


def test_invoice_processing_parallel_group_recorded(rt):
    iid = rt.start("invoice_processing", {"invoice_id": "INV7701", "sender": "edi"})
    groups = {
        a["status"] for a in store.rows("select status from wf_audit where instance_id=? and kind='parallel'", (iid,))
    }
    assert groups == {"parallel_po_and_contract"}
