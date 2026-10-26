"""Deterministic services of the production purchase workflow (section 3)."""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal
from typing import Any

from pev2 import config
from pev2.errors import BusinessException, LimitError
from pev2.services.common import Tools, money, to_eur
from pev2.tools import limits


def receive_requirement(t: Tools, *, requirement: dict[str, Any]) -> dict[str, Any]:
    for key in ("material_id", "quantity", "required_date", "requester"):
        if not requirement.get(key):
            raise BusinessException("incomplete_requirement", f"The requirement lacks {key}.")
    return {"requirement": {**requirement, "quantity": str(Decimal(str(requirement["quantity"])))}}


def validate_component(t: Tools, *, requirement: dict[str, Any]) -> dict[str, Any]:
    """Material must exist in the material master; the specification revision must be current."""
    try:
        mat = t("sap.get_material", material_id=requirement["material_id"])
    except BusinessException as exc:
        raise BusinessException("unknown_material", exc.detail) from exc
    if mat["specifications"].get("revision") != "current":
        raise BusinessException("obsolete_specification", f"The specification of {mat['id']} is not current.")
    return {
        "validated_requirement": {
            **requirement,
            "description": mat["description"],
            "category": mat["category"],
            "unit": mat["unit"],
        }
    }


def check_coverage(t: Tools, *, requirement: dict[str, Any]) -> dict[str, Any]:
    """Exclude reserved stock; count existing orders only when their open quantity and expected date cover the
    requirement."""
    stock = t("sap.get_stock", material_id=requirement["material_id"], plant=requirement.get("plant"))
    available = Decimal(stock["available"]) - Decimal(stock["reserved"])
    open_pos = t("sap.find_purchase_orders", material_id=requirement["material_id"], status="released")[
        "purchase_orders"
    ]
    covering = [p for p in open_pos if p.get("delivery_date") and p["delivery_date"] <= requirement["required_date"]]
    covered_qty = available + sum(
        Decimal(str(line["quantity"]))
        for p in covering
        for line in t("sap.get_purchase_order", po_number=p["id"])["items"]
        if line["material_id"] == requirement["material_id"]
    )
    need = Decimal(str(requirement["quantity"]))
    return {
        "coverage": {
            "available_stock": str(available),
            "open_order_quantity": str(covered_qty - available),
            "required": str(need),
            "shortfall": str(max(need - covered_qty, Decimal(0))),
            "covered": covered_qty >= need,
        }
    }


def identify_approved_suppliers(t: Tools, *, requirement: dict[str, Any]) -> dict[str, Any]:
    """Only suppliers on the approved list for the material group; blocked suppliers excluded; an empty set is an
    exception, never an unapproved substitute."""
    from pev2 import store

    rows = store.rows(
        "select a.supplier_id, s.name, s.blocked, p.lead_time_days, p.restrictions, p.late_deliveries from sap_approved_supplier a join sap_supplier s on s.id = a.supplier_id left join sap_supplier_profile p on p.supplier_id = a.supplier_id where a.material_category = ?",
        (requirement["category"],),
    )
    candidates = []
    for r in rows:
        sup = t("sap.get_supplier", supplier_id=r["supplier_id"])
        if sup["blocked"]:
            continue
        has_price = any(
            p["material_id"] == requirement["material_id"]
            for p in (
                store.rows("select material_id from sap_contract_price where supplier_id = ?", (r["supplier_id"],))
            )
        )
        if not has_price:
            continue
        candidates.append(
            {
                "supplier_id": r["supplier_id"],
                "name": r["name"],
                "lead_time_days": r["lead_time_days"],
                "restrictions": r["restrictions"] or "",
                "late_deliveries": r["late_deliveries"],
            }
        )
    if not candidates:
        raise BusinessException(
            "no_approved_supplier",
            f"No approved, unblocked supplier for {requirement['material_id']} ({requirement['category']}).",
        )
    return {"candidate_suppliers": candidates}


def retrieve_contract_conditions(t: Tools, *, candidates: list[dict[str, Any]], material_id: str) -> dict[str, Any]:
    conditions = []
    for c in candidates:
        try:
            contract = t("sap.get_contract", supplier_id=c["supplier_id"], material_id=material_id)
        except BusinessException:
            continue
        for p in contract["prices"]:
            if p["material_id"] == material_id and p["valid"]:
                conditions.append(
                    {
                        "supplier_id": c["supplier_id"],
                        "price": p["price"],
                        "currency": p["currency"],
                        "contract_ref": p["contract_ref"],
                        "valid_to": p["valid_to"],
                    }
                )
    if not conditions:
        raise BusinessException(
            "no_valid_contract", f"No valid contract price for {material_id} with any approved supplier."
        )
    return {"contract_conditions": conditions}


def select_supplier_and_prepare_po(
    t: Tools, *, requirement: dict[str, Any], evaluation: dict[str, Any], conditions: list[dict[str, Any]]
) -> dict[str, Any]:
    """Only an eligible, approved, unblocked supplier; respect single-source restrictions; prefer a valid contract
    supplier; lowest total cost among suppliers meeting the required date; verified prices only."""
    eligible = [e for e in evaluation.get("evaluations", []) if e.get("meets_date") and e.get("eligible")]
    if not eligible:
        raise BusinessException(
            "no_eligible_supplier", "No approved supplier can deliver by the required date within the restrictions."
        )
    prices = {c["supplier_id"]: c for c in conditions}
    single = [e for e in eligible if "single-source" in str(e.get("restrictions", "")).lower()]
    pool = single or eligible
    priced = [e for e in pool if e["supplier_id"] in prices]
    if not priced:
        raise BusinessException("missing_price", "No verified contract price for an eligible supplier.")
    qty = Decimal(str(requirement["quantity"]))
    chosen = min(priced, key=lambda e: Decimal(str(prices[e["supplier_id"]]["price"])) * qty)
    price = Decimal(str(prices[chosen["supplier_id"]]["price"]))
    total = money(price * qty)
    draft = {
        "supplier_id": chosen["supplier_id"],
        "supplier_name": chosen.get("name", chosen["supplier_id"]),
        "material_id": requirement["material_id"],
        "quantity": str(qty),
        "price": str(price),
        "currency": prices[chosen["supplier_id"]]["currency"],
        "total": str(total),
        "total_eur": str(to_eur(total, prices[chosen["supplier_id"]]["currency"])),
        "contract_ref": prices[chosen["supplier_id"]]["contract_ref"],
        "required_date": requirement["required_date"],
        "requester": requirement["requester"],
        "plant": requirement.get("plant", ""),
        "version": 1,
    }
    return {"selected_supplier": chosen["supplier_id"], "draft_purchase_order": draft}


def check_authorization(t: Tools, *, draft: dict[str, Any]) -> dict[str, Any]:
    """Buyer may release up to and including 25,000 EUR; above that the Procurement Manager approves; the
    authorization is bound to amount, currency and draft version."""
    total_eur = Decimal(str(draft["total_eur"]))
    needs = total_eur > limits.PO_APPROVAL_LIMIT_EUR
    return {
        "authorization_result": {
            "within_buyer_limit": not needs,
            "requires_approval": needs,
            "limit_eur": str(limits.PO_APPROVAL_LIMIT_EUR),
            "total_eur": str(total_eur),
            "bound_to": {"total": draft["total"], "currency": draft["currency"], "version": draft["version"]},
        }
    }


def submit_purchase_order(
    t: Tools, *, draft: dict[str, Any], authorization: dict[str, Any], approval_ref: str | None
) -> dict[str, Any]:
    """Revalidate supplier status and authority immediately before the write; submit only the unchanged draft;
    idempotency key; above the limit only with the approval."""
    sup = t("sap.get_supplier", supplier_id=draft["supplier_id"])
    if sup["blocked"]:
        raise LimitError(f"Supplier {sup['id']} is blocked; the order cannot be submitted.")
    if authorization["bound_to"]["total"] != draft["total"] or authorization["bound_to"]["version"] != draft["version"]:
        raise LimitError("The draft changed after authorization; a new authorization check is required.")
    if authorization["requires_approval"] and not approval_ref:
        raise LimitError("Orders above the buyer limit need the Procurement Manager's approval.")
    po = t(
        "sap.create_purchase_order",
        idempotency_key=f"{t.instance_id}:po:{draft['supplier_id']}:v{draft['version']}",  # one order per supplier and draft version
        approval_ref=approval_ref,
        supplier_id=draft["supplier_id"],
        items=[{"material_id": draft["material_id"], "quantity": draft["quantity"], "price": draft["price"]}],
        currency=draft["currency"],
        total=draft["total"],
        total_eur=draft["total_eur"],
        authorization_ok=True,
        requester=draft["requester"],
        plant=draft["plant"],
        required_date=draft["required_date"],
    )
    return {
        "purchase_order": {
            **po,
            **{
                k: draft[k]
                for k in ("supplier_id", "material_id", "quantity", "price", "total", "currency", "required_date")
            },
        }
    }


def track_confirmation(t: Tools, *, po_number: str, requirement: dict[str, Any]) -> dict[str, Any]:
    """Correlate the confirmation to the order; rejection, unacceptable date or quantity, unavailability and timeout
    go to exception handling."""
    conf = t("sap.get_supplier_confirmation", po_number=po_number)
    status = conf.get("status", "pending")
    if status == "pending":
        return {"confirmation_status": {"status": "pending"}}
    acceptable = status == "confirmed" and (
        not conf.get("confirmed_date") or conf["confirmed_date"] <= requirement["required_date"]
    )
    return {
        "confirmation_status": {
            "status": status,
            "confirmed_date": conf.get("confirmed_date"),
            "note": conf.get("note", ""),
            "acceptable": acceptable,
        }
    }


def notify_requester(t: Tools, *, requirement: dict[str, Any], message: str) -> dict[str, Any]:
    draft = t(
        "email.create_draft",
        idempotency_key=f"{t.instance_id}:req-draft",
        to=[f"{requirement['requester'].lower().replace(' ', '.')}@danishcrown.example"],
        subject=f"Your requirement for {requirement['material_id']}",
        body=message,
        instance_id=t.instance_id,
    )
    sent = t(
        "email.send_approved",
        idempotency_key=f"{t.instance_id}:req-send",
        draft_id=draft["draft_id"],
        policy="payment-answer",
    )
    return {"requester_notification": sent}


def required_date_feasible(lead_time_days: int, required_date: str, today: date | None = None) -> bool:
    return (today or config.today()) + timedelta(days=int(lead_time_days)) <= date.fromisoformat(required_date)
