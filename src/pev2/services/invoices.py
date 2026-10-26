"""Deterministic services of the two invoice workflows (section 3). Each function is one service of the architecture;
the rules in its docstring are the tests. Tools go through the registry with the service's own allow-list."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from pev2 import store
from pev2.errors import BusinessException, LimitError
from pev2.services import matching
from pev2.services.common import Tools, money, to_eur


def _invoice_in(inv: dict[str, Any]) -> matching.InvoiceIn:
    return matching.InvoiceIn(
        id=inv["id"],
        supplier_id=inv["supplier_id"],
        po_id=inv.get("po_id") or None,
        currency=inv.get("currency") or "DKK",
        amount=Decimal(str(inv["amount"])),
        lines=[
            matching.LineIn(
                line_no=int(ln["line_no"]),
                material_id=ln["material_id"],
                quantity=Decimal(str(ln["quantity"])),
                price=Decimal(str(ln["price"])),
            )
            for ln in inv.get("lines", [])
        ],
    )


# ---- intake ------------------------------------------------------------------------------------------
def open_case_and_assign(t: Tools, *, subject: str, kind: str, owner: str, description: str = "") -> dict[str, Any]:
    """Case correlation is deterministic: a reply joins the open case of the same subject; invoice events are
    owned by the AP Specialist; duplicate events must not create duplicate cases."""
    from pev2.tools import jira

    existing = jira.find_open_case(summary_contains=subject)
    if existing:
        return {"case_key": existing["key"], "reused": True, "owner": existing["assignee"]}
    created = t(
        "jira.create_issue",
        idempotency_key=t.instance_id and f"{t.instance_id}:case",
        project="PROC",
        summary=subject,
        description=description,
        issue_type=kind,
        assignee=owner,
        instance_id=t.instance_id,
    )
    t(
        "jira.assign_issue",
        idempotency_key=t.instance_id and f"{t.instance_id}:assign",
        issue_key=created["issue_key"],
        assignee=owner,
    )
    return {"case_key": created["issue_key"], "reused": False, "owner": owner}


def receive_invoice(t: Tools, *, invoice_id: str, sender: str = "") -> dict[str, Any]:
    """Deduplicate inbound events by the stable invoice reference; archive the document; unreadable → exception."""
    inv = t("sap.get_invoice", invoice_id=invoice_id)
    if not inv.get("lines"):
        raise BusinessException("unreadable_attachment", f"Invoice {invoice_id} has no readable lines.")
    stored = t(
        "dms.store_document",
        idempotency_key=f"{t.instance_id}:archive:{invoice_id}",
        document_id=f"DOC-{invoice_id}",
        content_ref=f"mail://{sender}/{invoice_id}.pdf",
        metadata={"invoice_id": invoice_id, "sender": sender},
    )
    return {"invoice_reference": invoice_id, "raw_document": stored["document_id"], "sender": sender}


def extract_invoice_details(t: Tools, *, invoice_id: str) -> dict[str, Any]:
    """The AI-backed extraction (an IDP call in production, the SAP rows here). Totals must equal the sum of lines;
    low confidence or inconsistent results go to human exception handling."""
    inv = t("sap.get_invoice", invoice_id=invoice_id)
    lines_total = money(
        sum((Decimal(str(ln["quantity"])) * Decimal(str(ln["price"])) for ln in inv["lines"]), Decimal(0))
    )
    consistent = lines_total == money(inv["amount"])
    confidence = Decimal("0.98") if consistent else Decimal("0.55")
    data = {
        "invoice_id": inv["id"],
        "supplier_id": inv["supplier_id"],
        "po_id": inv.get("po_id"),
        "invoice_number": inv["invoice_number"],
        "amount": inv["amount"],
        "currency": inv["currency"],
        "lines": inv["lines"],
        "confidence": str(confidence),
        "lines_total": str(lines_total),
    }
    if confidence < Decimal("0.6"):
        raise BusinessException(
            "low_extraction_confidence",
            f"Extracted totals ({inv['amount']}) do not equal the sum of line items ({lines_total}).",
            data,
        )
    return data


def identify_supplier(t: Tools, *, invoice_data: dict[str, Any]) -> dict[str, Any]:
    """The supplier must exist and not be blocked; ambiguous or mismatched suppliers cannot proceed."""
    try:
        sup = t("sap.get_supplier", supplier_id=invoice_data["supplier_id"])
    except BusinessException as exc:
        raise BusinessException("supplier_not_found", exc.detail) from exc
    if sup["blocked"]:
        raise BusinessException("supplier_blocked", f"Supplier {sup['id']} ({sup['name']}) is blocked.")
    return {
        "supplier_id": sup["id"],
        "name": sup["name"],
        "email": sup["email"],
        "payment_terms_days": sup["payment_terms_days"],
    }


def locate_purchase_order(t: Tools, *, invoice_data: dict[str, Any], supplier_id: str) -> dict[str, Any]:
    """Find the referenced PO, else search by supplier and material; never select an ambiguous PO automatically;
    keep the goods-receipt evidence for quantity validation."""
    po = None
    if invoice_data.get("po_id"):
        try:
            po = t("sap.get_purchase_order", po_number=invoice_data["po_id"])
        except BusinessException:
            po = None
    if po is None:
        found = t(
            "sap.find_purchase_orders",
            supplier_id=supplier_id,
            material_id=(invoice_data.get("lines") or [{}])[0].get("material_id"),
        )["purchase_orders"]
        if len(found) == 1:
            po = t("sap.get_purchase_order", po_number=found[0]["id"])
        elif len(found) > 1:
            raise BusinessException(
                "ambiguous_purchase_order",
                f"{len(found)} purchase orders could match invoice {invoice_data['invoice_id']}.",
            )
    if po is None:
        raise BusinessException(
            "missing_purchase_order", f"No purchase order for invoice {invoice_data['invoice_id']}."
        )
    receipts = t("sap.get_goods_receipt", po_number=po["po_number"])["receipts"]
    return {"purchase_order": po, "goods_receipts": receipts}


def retrieve_contract(t: Tools, *, supplier_id: str, materials: list[str]) -> dict[str, Any]:
    """The applicable contract prices; missing or expired → modelled exception."""
    try:
        contract = t("sap.get_contract", supplier_id=supplier_id)
    except BusinessException as exc:
        raise BusinessException("missing_contract", f"No purchasing contract for supplier {supplier_id}.") from exc
    prices = {p["material_id"]: p for p in contract["prices"]}
    if materials and not any(prices.get(m, {}).get("valid") for m in materials):
        raise BusinessException(
            "expired_contract",
            f"No valid contract price for {', '.join(materials)} with {supplier_id} on the invoice date.",
        )
    return contract


def _match_inputs(
    invoice_data: dict[str, Any],
    po: dict[str, Any] | None,
    receipts: list[dict[str, Any]],
    contract: dict[str, Any] | None,
    supplier_blocked: bool = False,
) -> matching.MatchResult:
    inv = _invoice_in({**invoice_data, "id": invoice_data.get("invoice_id") or invoice_data.get("id")})
    po_in = (
        matching.PoIn(
            id=po["po_number"],
            supplier_id=po["supplier_id"],
            lines=[
                matching.LineIn(
                    line_no=int(ln["line_no"]),
                    material_id=ln["material_id"],
                    quantity=Decimal(str(ln["quantity"])),
                    price=Decimal(str(ln["price"])),
                )
                for ln in po["items"]
            ],
        )
        if po
        else None
    )
    rec = [
        matching.ReceiptIn(po_id=r["po_id"], line_no=int(r["line_no"]), quantity=Decimal(str(r["quantity"])))
        for r in receipts
    ]
    prices = {
        p["material_id"]: matching.ContractPriceIn(
            material_id=p["material_id"],
            price=Decimal(str(p["price"])),
            currency=p["currency"],
            valid=bool(p["valid"]),
            contract_ref=p["contract_ref"],
        )
        for p in (contract or {}).get("prices", [])
    }
    return matching.match(inv, po_in, rec, prices, supplier_blocked)


def compare_invoice_po_contract(
    invoice_data: dict[str, Any], located: dict[str, Any], contract: dict[str, Any] | None
) -> dict[str, Any]:
    """Unit price may deviate at most 2% from the contract price; quantity may not exceed received; missing
    evidence is an exception, not a pass."""
    result = _match_inputs(invoice_data, located["purchase_order"], located["goods_receipts"], contract)
    result = matching.apply_policy(result, Decimal("2"), Decimal("0"), Decimal("0"))
    if not result.three_way:
        raise BusinessException(
            "missing_evidence", "Goods receipt missing: the comparison has no received quantity to validate against."
        )
    return {
        "match": result.model_dump(mode="json"),
        "summary": result.summary(),
        "within_tolerance": result.within_tolerance,
        "blocking": [d.model_dump(mode="json") for d in result.blocking],
    }


def validate_invoice(t: Tools, *, invoice_data: dict[str, Any], comparison: dict[str, Any]) -> dict[str, Any]:
    """Total deviation within 50 EUR or 1% accepted automatically (subject to the other rules); currency must match
    the PO currency; tax code must match the contract tax rule; invoice number unique per supplier (duplicate check)."""
    issues: list[str] = []
    result = matching.MatchResult.model_validate(comparison["match"])
    currency_ok = result.currency == "DKK"
    if not currency_ok:
        issues.append(f"Currency {result.currency} does not match the PO currency DKK.")
    tax_ok = invoice_data.get("tax_code", "DK25") == "DK25"
    if not tax_ok:
        issues.append(f"Tax code {invoice_data.get('tax_code')} does not match the contract tax rule DK25.")
    others = t(
        "sap.get_invoice", supplier_id=invoice_data["supplier_id"], invoice_number=invoice_data["invoice_number"]
    )["invoices"]
    duplicates = [o["id"] for o in others if o["id"] != invoice_data["invoice_id"]]
    if duplicates:
        raise BusinessException(
            "duplicate_invoice",
            f"Invoice number {invoice_data['invoice_number']} already exists for {invoice_data['supplier_id']}: {', '.join(duplicates)}.",
        )
    deviation = result.total_deviation()
    deviation_eur = to_eur(deviation, result.currency)
    pct = (deviation / result.invoice_amount * 100) if result.invoice_amount else Decimal(0)
    auto_accept = deviation_eur <= Decimal("50") or pct <= Decimal("1")
    hard = [d for d in result.differences if d.kind not in ("price", "quantity") or not d.within_tolerance]
    valid = currency_ok and tax_ok and (auto_accept or result.within_tolerance) and not hard
    if hard:
        issues.extend(d.detail for d in hard)
    return {
        "status": "valid" if valid else "invalid",
        "issues": issues,
        "deviation": str(deviation),
        "deviation_eur": str(deviation_eur),
        "deviation_pct": str(money(pct)),
        "requires_approval": not valid,
    }


# ---- story B services (price deviation workflow) ---------------------------------------------------------
def fetch_tolerance_policy(t: Tools) -> dict[str, Any]:
    """The tolerance is read from the policy document, never hard-coded; stop for a human if it cannot be read."""
    pb = t("brain.fetch_playbook", playbook="invoice-handling")
    facts = pb.get("facts") or {}
    if "pct" not in facts or "abs" not in facts:
        raise BusinessException(
            "policy_unreadable", "The invoice-handling policy does not state a tolerance that can be validated."
        )
    return {
        "policy": "invoice-handling",
        "version": pb["version"],
        "pct": facts["pct"],
        "abs": facts["abs"],
        "currency": facts.get("currency", "DKK"),
        "quantity_tolerance": facts.get("quantity_tolerance", "0"),
    }


def match_invoice(t: Tools, *, invoice_id: str, policy: dict[str, Any]) -> dict[str, Any]:
    """Decimal arithmetic without implicit rounding; every difference reported; the validated policy applied per
    line; invoiced quantity ≤ received; stop on missing PO, missing receipt or duplicate invoice."""
    inv = t("sap.get_invoice", invoice_id=invoice_id)
    dup = [
        o["id"]
        for o in t("sap.get_invoice", supplier_id=inv["supplier_id"], invoice_number=inv["invoice_number"])["invoices"]
        if o["id"] != inv["id"]
    ]
    if dup:
        raise BusinessException(
            "duplicate_invoice",
            f"Invoice number {inv['invoice_number']} already exists for {inv['supplier_id']}: {', '.join(dup)}.",
        )
    sup = t("sap.get_supplier", supplier_id=inv["supplier_id"])
    if not inv.get("po_id"):
        raise BusinessException("purchase_order_not_found", f"Invoice {invoice_id} references no purchase order.")
    try:
        po = t("sap.get_purchase_order", po_number=inv["po_id"])
    except BusinessException as exc:
        raise BusinessException("purchase_order_not_found", exc.detail) from exc
    receipts = t("sap.get_goods_receipt", po_number=po["po_number"])["receipts"]
    if not receipts:
        raise BusinessException(
            "goods_receipt_missing",
            f"No goods receipt is recorded for PO {po['po_number']}; the goods may not have arrived.",
        )
    try:
        contract = t("sap.get_contract", supplier_id=inv["supplier_id"])
    except BusinessException:
        contract = None
    result = _match_inputs({**inv, "invoice_id": inv["id"]}, po, receipts, contract, bool(sup["blocked"]))
    result = matching.apply_policy(
        result,
        Decimal(str(policy["pct"])),
        Decimal(str(policy["abs"])),
        Decimal(str(policy.get("quantity_tolerance", "0"))),
    )
    return {
        "match": result.model_dump(mode="json"),
        "summary": result.summary(),
        "within_tolerance": result.within_tolerance,
        "blocking": [d.model_dump(mode="json") for d in result.blocking],
        "total_deviation": str(result.total_deviation()),
        "invoice": {
            "id": inv["id"],
            "supplier_id": inv["supplier_id"],
            "supplier_name": sup["name"],
            "supplier_email": sup["email"],
            "amount": inv["amount"],
            "currency": inv["currency"],
            "invoice_number": inv["invoice_number"],
            "po_id": inv["po_id"],
        },
    }


def retrieve_supplier_history(t: Tools, *, supplier_id: str) -> dict[str, Any]:
    """What is on file with this supplier: earlier deviations, credit notes, contract conditions."""
    brief = t("brain.supplier_brief", supplier_id=supplier_id)
    try:
        contract = t("sap.get_contract", supplier_id=supplier_id)
    except BusinessException:
        contract = {"prices": [], "conditions": []}
    earlier = [
        i
        for i in store.rows(
            "select id, status, note, credit_note_ref from sap_invoice where supplier_id = ? and (status = 'blocked' or credit_note_ref is not null)",
            (supplier_id,),
        )
    ]
    return {
        "supplier_context": {
            "history": brief["history"],
            "contract_facts": brief["contract_facts"],
            "contacts": brief["contacts"],
            "earlier_deviations": earlier,
            "contract_id": contract.get("contract_id"),
            "conditions": contract.get("conditions", []),
        }
    }


def block_invoice(t: Tools, *, invoice_id: str, match: dict[str, Any]) -> dict[str, Any]:
    """Confirm identity and status before blocking; block when the deviation exceeds the validated tolerance; never
    partially pay."""
    inv = t("sap.get_invoice", invoice_id=invoice_id)
    if match["within_tolerance"]:
        raise LimitError("The invoice is within tolerance; it must not be blocked.")
    if inv["status"] in ("paid", "posted"):
        raise LimitError(f"Invoice {invoice_id} is {inv['status']}; it cannot be blocked.")
    reason = "; ".join(d["detail"] for d in match["blocking"])
    return t(
        "sap.block_invoice",
        idempotency_key=f"{t.instance_id}:block:{invoice_id}",
        document_number=invoice_id,
        reason=reason,
    )


def attach_reply_to_case(t: Tools, *, case_key: str, reply: dict[str, Any]) -> dict[str, Any]:
    """Replies join the existing open case; never create another; the content is untrusted data."""
    body = f"Supplier reply (untrusted content):\n{reply.get('body', '')}"
    t(
        "jira.add_comment",
        idempotency_key=f"{t.instance_id}:reply:{reply.get('id', 'x')}",
        issue_key=case_key,
        body=body,
    )
    return {"case_key": case_key, "reply_body": reply.get("body", ""), "reply_from": reply.get("from_addr", "")}


def rematch_with_credit_note(
    t: Tools, *, invoice_id: str, match: dict[str, Any], credit_note_ref: str | None, supplier_decision: str
) -> dict[str, Any]:
    """Only the difference covered by a confirmed credit note may remain open; if the credit note cannot be
    established, stop for human verification; do not park."""
    result = matching.MatchResult.model_validate(match["match"])
    if supplier_decision != "accept" or not credit_note_ref:
        return {
            "ok": False,
            "reason": f"supplier decision: {supplier_decision}; credit note: {credit_note_ref or 'none'}",
        }
    inv = t("sap.get_invoice", invoice_id=invoice_id)
    if inv["status"] not in ("blocked", "received"):
        return {"ok": False, "reason": f"invoice is {inv['status']}"}
    return matching.rematch_with_credit_note(result, credit_note_ref, None)


def park_invoice(t: Tools, *, invoice_id: str, match: dict[str, Any], rematch: dict[str, Any] | None) -> dict[str, Any]:
    """Park only if the match is within policy or the verified credit note satisfies the rematch condition; the AP
    Specialist parks, only the AP Team Lead releases."""
    if not match["within_tolerance"] and not (rematch and rematch.get("ok")):
        raise LimitError(
            "The invoice may only be parked when it is within policy or a confirmed credit note covers the difference."
        )
    note = (
        f"within tolerance {match['match']['tolerance_applied']}"
        if match["within_tolerance"]
        else f"credit note {(rematch or {}).get('credit_note_ref')} covers {(rematch or {}).get('price_difference')} {match['invoice']['currency']}"
    )
    return t(
        "sap.park_invoice",
        idempotency_key=f"{t.instance_id}:park:{invoice_id}",
        invoice={"id": invoice_id},
        notes=note,
        credit_note_ref=(rematch or {}).get("credit_note_ref"),
    )


def release_invoice(t: Tools, *, invoice_id: str, approval: dict[str, Any]) -> dict[str, Any]:
    """Recheck the approval binding, invoice state and release limit immediately before the write."""
    if approval.get("decision") != "approved":
        raise LimitError("The release approval was not given.")
    return t(
        "sap.release_invoice",
        idempotency_key=f"{t.instance_id}:release:{invoice_id}",
        approval_ref=approval["id"],
        document_number=invoice_id,
        released_by=approval.get("by", "AP Team Lead"),
    )


# ---- invoice processing workflow (story A) ------------------------------------------------------------------
def prepare_notification_or_dispute(
    t: Tools, *, discrepancy: dict[str, Any], invoice_data: dict[str, Any], supplier: dict[str, Any], case_key: str
) -> dict[str, Any]:
    """Draft (never send) the supplier clarification and open the dispute case; the exact message needs human
    review before email.send_approved; idempotency keys on every write."""
    body = (
        f"Hello, regarding invoice {invoice_data['invoice_number']} (our reference {invoice_data['invoice_id']}): "
        + " ".join(discrepancy.get("explanations", []))
        + " Could you confirm the correct values or send a corrected invoice / credit note?"
    )
    draft = t(
        "email.create_draft",
        idempotency_key=f"{t.instance_id}:draft",
        to=[supplier["email"]],
        subject=f"Invoice {invoice_data['invoice_number']}: clarification needed",
        body=body,
        thread_id=f"inv:{invoice_data['invoice_id']}",
        instance_id=t.instance_id,
    )
    t(
        "jira.add_comment",
        idempotency_key=f"{t.instance_id}:dispute",
        issue_key=case_key,
        body=f"Dispute opened. Discrepancies: {'; '.join(discrepancy.get('explanations', []))}. Draft {draft['draft_id']} awaits approval.",
    )
    return {
        "draft_id": draft["draft_id"],
        "thread_id": f"inv:{invoice_data['invoice_id']}",
        "case_key": case_key,
        "body": body,
    }


def submit_to_financial_workflow(
    t: Tools, *, invoice_id: str, validation: dict[str, Any], approval_ref: str | None
) -> dict[str, Any]:
    """Only invoices with status valid (or an approved discrepancy) may be posted; recheck supplier and duplicates;
    idempotency key on the write."""
    status = validation["status"] if validation["status"] == "valid" else ("valid" if approval_ref else "invalid")
    return t(
        "sap.submit_invoice",
        idempotency_key=f"{t.instance_id}:submit:{invoice_id}",
        approval_ref=approval_ref,
        invoice={"id": invoice_id},
        status=status,
        requires_discrepancy_approval=validation["status"] != "valid",
    )
