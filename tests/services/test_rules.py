"""The business rules of section 3, one test per rule, written from the rule text."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from pev2.errors import BusinessException, LimitError
from pev2.services import enquiries, invoices, matching, payments, purchasing
from pev2.services.common import Tools
from pev2.tools import brain


def _inv(price="52.00", qty="100", amount="5200.00", po="PO45088"):
    return matching.InvoiceIn(
        id="INV7731",
        supplier_id="S1004",
        po_id=po,
        amount=Decimal(amount),
        lines=[matching.LineIn(line_no=1, material_id="M2110", quantity=Decimal(qty), price=Decimal(price))],
    )


PO = matching.PoIn(
    id="PO45088",
    supplier_id="S1004",
    lines=[matching.LineIn(line_no=1, material_id="M2110", quantity=Decimal("100"), price=Decimal("48.00"))],
)
GR = [matching.ReceiptIn(po_id="PO45088", line_no=1, quantity=Decimal("100"))]
CP = {
    "M2110": matching.ContractPriceIn(
        material_id="M2110", price=Decimal("48.00"), valid=True, contract_ref="C-2026-021"
    )
}


def test_every_difference_is_reported_with_decimal_arithmetic():
    r = matching.match(_inv(), PO, GR, CP)
    assert [d.kind for d in r.differences] == ["price"]
    d = r.differences[0]
    assert d.total_delta == Decimal("400.00") and d.delta_pct == Decimal("8.33") and d.contract_ref == "C-2026-021"


def test_tolerance_one_percent_or_200_dkk_whichever_higher():
    r = matching.apply_policy(
        matching.match(_inv(price="49.50", amount="4950.00"), PO, GR, CP), Decimal("1"), Decimal("200")
    )
    assert r.within_tolerance  # 150 DKK on a 4,800 line: under 200 DKK
    r = matching.apply_policy(matching.match(_inv(), PO, GR, CP), Decimal("1"), Decimal("200"))
    assert not r.within_tolerance  # 400 DKK
    r = matching.apply_policy(matching.match(_inv(), PO, GR, CP), Decimal("10"), Decimal("200"))
    assert r.within_tolerance  # the closing moment: 10% tolerates it


def test_quantity_may_not_exceed_received():
    r = matching.apply_policy(
        matching.match(_inv(price="48.00", qty="101", amount="4848.00"), PO, GR, CP),
        Decimal("1"),
        Decimal("200"),
        Decimal("0"),
    )
    assert {d.kind for d in r.differences} == {"quantity", "quantity_over_po"} and not r.within_tolerance


def test_missing_goods_receipt_is_not_a_match():
    r = matching.match(_inv(price="48.00", amount="4800.00"), PO, [], CP)
    assert [d.kind for d in r.differences] == ["not_received"] and not r.three_way


def test_rematch_only_covered_difference_may_remain():
    r = matching.apply_policy(matching.match(_inv(), PO, GR, CP), Decimal("1"), Decimal("200"))
    assert matching.rematch_with_credit_note(r, "CN-2026-118", None)["ok"]
    assert not matching.rematch_with_credit_note(r, None, None)["ok"]
    assert not matching.rematch_with_credit_note(r, "CN-1", Decimal("100"))["ok"]


def test_policy_document_drives_the_tolerance(world):
    facts = brain.parse_tolerance((brain.DOCS / "policies" / "invoice-handling.md").read_text())
    assert (
        facts["pct"] == Decimal("1") and facts["abs"] == Decimal("200") and facts["quantity_tolerance"] == Decimal("0")
    )
    assert facts["payment_terms_days"] == 30 and facts["payment_run_weekday"] == "thursday"
    assert brain.parse_tolerance("up to 10% of the contract line value or 200 DKK")["pct"] == Decimal("10")


def test_due_date_and_payment_run_in_code():
    assert payments.due_date(date(2026, 10, 4), 30) == date(2026, 11, 3)
    assert payments.expected_payment_run(date(2026, 11, 3), date(2026, 10, 20), "thursday") == date(2026, 11, 5)
    assert payments.expected_payment_run(date(2026, 10, 1), date(2026, 10, 20), "thursday") == date(2026, 10, 22), (
        "never in the past"
    )
    text = payments.draft_payment_answer(
        {"document_number": "INV7702", "currency": "DKK", "amount": "23524.00"},
        {"terms_days": "30", "due_date": "2026-11-03", "expected_payment_run": "2026-11-05"},
        "2026-0912",
    )
    assert "expectation, not a promise" in text and "Thursday 5 November" in text


def test_duplicate_invoice_stops_matching(world):
    from pev2 import store

    store.run(
        "insert into sap_invoice (id, supplier_id, po_id, invoice_number, amount, currency, status) values ('INV9999', 'S1004', 'PO45088', '829884', '1', 'DKK', 'received')"
    )
    with pytest.raises(BusinessException) as exc:
        invoices.match_invoice(
            Tools("service.match_invoice", "ap_specialist", "i1"),
            invoice_id="INV7731",
            policy={"pct": "1", "abs": "200"},
        )
    assert exc.value.kind == "duplicate_invoice"


def test_block_refused_within_tolerance_and_release_needs_approval(world):
    t = Tools("service.block_invoice", "ap_specialist", "i1")
    with pytest.raises(LimitError):
        invoices.block_invoice(t, invoice_id="INV7731", match={"within_tolerance": True, "blocking": []})
    with pytest.raises(LimitError):
        invoices.release_invoice(
            Tools("service.release_invoice", "ap_team_lead", "i1"),
            invoice_id="INV7731",
            approval={"decision": "approved", "id": "nope"},
        )
    with pytest.raises(LimitError):  # segregation of duties: the specialist may not release
        Tools("service.release_invoice", "ap_specialist", "i1")(
            "sap.release_invoice", idempotency_key="k", document_number="INV7731"
        )


def test_po_above_limit_needs_procurement_manager(world):
    draft = {
        "supplier_id": "S1001",
        "material_id": "M2001",
        "quantity": "10000",
        "price": "38.50",
        "currency": "DKK",
        "total": "385000.00",
        "total_eur": "51677.85",
        "required_date": "2026-11-30",
        "requester": "x",
        "plant": "Horsens",
        "version": 1,
    }
    auth = purchasing.check_authorization(Tools("service.check_authorization", "buyer", "i1"), draft=draft)[
        "authorization_result"
    ]
    assert auth["requires_approval"]
    with pytest.raises(LimitError):
        purchasing.submit_purchase_order(
            Tools("service.submit_purchase_order", "buyer", "i1"), draft=draft, authorization=auth, approval_ref=None
        )
    small = {**draft, "quantity": "100", "total": "3850.00", "total_eur": "516.78"}
    auth2 = purchasing.check_authorization(Tools("service.check_authorization", "buyer", "i1"), draft=small)[
        "authorization_result"
    ]
    assert not auth2["requires_approval"]


def test_only_approved_unblocked_suppliers(world):
    from pev2 import store

    t = Tools("service.identify_approved_suppliers", "buyer", "i1")
    cands = purchasing.identify_approved_suppliers(t, requirement={"material_id": "M2001", "category": "packaging"})[
        "candidate_suppliers"
    ]
    assert {c["supplier_id"] for c in cands} == {"S1001", "S1006"}
    store.run("update sap_supplier set blocked = 1 where id = 'S1001'")
    cands = purchasing.identify_approved_suppliers(t, requirement={"material_id": "M2001", "category": "packaging"})[
        "candidate_suppliers"
    ]
    assert [c["supplier_id"] for c in cands] == ["S1006"], "a blocked supplier is never a candidate"
    store.run("update sap_supplier set blocked = 1 where id = 'S1006'")
    with pytest.raises(BusinessException):  # an empty set is an exception, never an unapproved substitute
        purchasing.identify_approved_suppliers(t, requirement={"material_id": "M2001", "category": "packaging"})
    store.run("delete from sap_approved_supplier where material_category = 'packaging'")
    with pytest.raises(BusinessException):
        purchasing.identify_approved_suppliers(t, requirement={"material_id": "M2001", "category": "packaging"})


def test_unknown_sender_cannot_get_invoice_information(world):
    with pytest.raises(BusinessException):
        enquiries.identify_supplier_from_sender(
            Tools("service.identify_supplier_from_sender", "supplier_manager", "i1"), sender="nobody@example.com"
        )
    assert enquiries.classify_message("When will invoice 2026-0912 be paid?") == "payment_question"
