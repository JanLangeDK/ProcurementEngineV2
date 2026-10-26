"""Production purchase workflow (section 4): requirement → validation → coverage → approved suppliers and contract
conditions → supplier evaluation (agent) → selection by rules → authorization → approval above the limit → PO in SAP
→ confirmation tracking → exception handling by the agent → requester informed."""

from __future__ import annotations

from datetime import timedelta

from pev2.engine import Context, workflow
from pev2.errors import BusinessException
from pev2.gates import confirmation_acceptable, pre_submit_po, procurement_necessary
from pev2.gateway import run_agent
from pev2.services import purchasing
from pev2.services.common import Tools


def _t(ctx: Context, service: str, role: str = "buyer") -> Tools:
    return Tools(caller=service, role=role, instance_id=ctx.instance_id)


@workflow(
    "production_purchase",
    title="Production purchase request",
    trigger="Component requirement identified",
    end_states=["Requirement fulfilled", "Requirement cannot be fulfilled"],
)
def production_purchase(ctx: Context) -> str:
    req = ctx.step(
        "Receive component requirement",
        lambda: purchasing.receive_requirement(
            _t(ctx, "service.receive_requirement", "workflow"), requirement=ctx.trigger
        ),
        component="service.receive_requirement",
    )["requirement"]
    try:
        validated = ctx.step(
            "Validate component identification and specifications",
            lambda: purchasing.validate_component(_t(ctx, "service.validate_component"), requirement=req),
            component="service.validate_component",
        )["validated_requirement"]
    except BusinessException as exc:
        ctx.review("Validate component identification and specifications", f"{exc.kind}: {exc.detail}", exc.data)
        return "Requirement cannot be fulfilled"
    coverage = ctx.step(
        "Check inventory and existing purchase orders",
        lambda: purchasing.check_coverage(_t(ctx, "service.check_coverage"), requirement=validated),
        component="service.check_coverage",
    )["coverage"]
    ctx.gate("Procurement and confirmation branches", procurement_necessary(coverage))
    if (
        ctx.decision(
            "Procurement necessary?",
            "no" if coverage["covered"] else "yes",
            "available stock + open PO quantity < required quantity by the required date",
        )
        == "no"
    ):
        ctx.step(
            "Update the requesting production function",
            lambda: purchasing.notify_requester(
                _t(ctx, "service.notify_requester", "workflow"),
                requirement=validated,
                message=f"Your requirement for {validated['quantity']} {validated['unit']} of {validated['description']} is covered by stock and open orders ({coverage['available_stock']} on hand, {coverage['open_order_quantity']} on order). No new purchase order is needed.",
            ),
            component="service.notify_requester",
        )
        return "Requirement fulfilled"

    attempt = ctx.state.get("counter:sourcing", 0)
    try:
        candidates = ctx.step(
            f"Identify approved suppliers #{attempt}",
            lambda: purchasing.identify_approved_suppliers(
                _t(ctx, "service.identify_approved_suppliers"), requirement=validated
            ),
            component="service.identify_approved_suppliers",
        )["candidate_suppliers"]
        conditions = ctx.step(
            f"Retrieve contract conditions and agreed prices #{attempt}",
            lambda: purchasing.retrieve_contract_conditions(
                _t(ctx, "service.retrieve_contract_conditions"),
                candidates=candidates,
                material_id=validated["material_id"],
            ),
            component="tool.sap.get_contract",
            kind="tool",
        )["contract_conditions"]
    except BusinessException as exc:
        ctx.review("Identify approved suppliers", f"{exc.kind}: {exc.detail}", exc.data)
        return "Requirement cannot be fulfilled"
    excluded = ctx.state.get("excluded_suppliers", [])
    evaluation = ctx.step(
        f"Evaluate availability, lead time and purchasing restrictions #{attempt}",
        lambda: run_agent(
            "agent.supplier_reply_interpretation",
            {
                "validated_requirement": validated,
                "candidate_suppliers": [c for c in candidates if c["supplier_id"] not in excluded],
                "contract_conditions": conditions,
                "process_instance_id": ctx.instance_id,
                "objective": "Assess each candidate's ability to deliver by the required date",
            },
            ctx.instance_id,
        ),
        component="agent.supplier_reply_interpretation",
        kind="agent",
    )
    try:
        selection = ctx.step(
            f"Select supplier and prepare purchase order #{attempt}",
            lambda: purchasing.select_supplier_and_prepare_po(
                _t(ctx, "service.select_supplier"), requirement=validated, evaluation=evaluation, conditions=conditions
            ),
            component="service.select_supplier",
        )
    except BusinessException as exc:
        ctx.review("Select supplier according to business rules", f"{exc.kind}: {exc.detail}", exc.data)
        return "Requirement cannot be fulfilled"
    draft = selection["draft_purchase_order"]
    authorization = ctx.step(
        f"Check purchasing authorization and approval limits #{attempt}",
        lambda: purchasing.check_authorization(_t(ctx, "service.check_authorization"), draft=draft),
        component="service.check_authorization",
    )["authorization_result"]
    approval = None
    if authorization["requires_approval"]:
        approval = ctx.approval(
            f"Approve purchase order #{attempt}",
            "Procurement Manager",
            {
                "supplier": draft["supplier_name"],
                "material": draft["material_id"],
                "quantity": draft["quantity"],
                "total": f"{draft['total']} {draft['currency']} ({draft['total_eur']} EUR)",
                "required_date": draft["required_date"],
            },
            timeout_days=2,
            escalation="Head of Procurement",
        )
        if approval["decision"] != "approved":
            return "Requirement cannot be fulfilled"
    ctx.gate(f"Pre-check: Submit purchase order through SAP #{attempt}", pre_submit_po(draft, authorization, approval))
    po = ctx.step(
        f"Submit purchase order through SAP #{attempt}",
        lambda: purchasing.submit_purchase_order(
            _t(ctx, "service.submit_purchase_order"),
            draft=draft,
            authorization=authorization,
            approval_ref=approval["id"] if approval else None,
        ),
        component="service.submit_purchase_order",
    )["purchase_order"]
    confirmation_event = ctx.wait_event(
        f"Wait for: Supplier confirmation #{attempt}",
        "supplier_confirmation",
        po["po_number"],
        timeout=timedelta(days=3),
    )
    confirmation = ctx.step(
        f"Track supplier confirmation #{attempt}",
        lambda: purchasing.track_confirmation(
            _t(ctx, "service.track_confirmation"), po_number=po["po_number"], requirement=validated
        ),
        component="service.track_confirmation",
    )["confirmation_status"]
    if confirmation_event.get("timed_out"):
        confirmation = {**confirmation, "status": "timeout", "acceptable": False}
    ctx.gate(f"Confirmation branch #{attempt}", confirmation_acceptable(confirmation, validated))
    if (
        ctx.decision(
            f"Confirmed as required? #{attempt}",
            "confirmed" if confirmation.get("acceptable") else "problem",
            "supplier confirmed quantity and date",
        )
        == "confirmed"
    ):
        ctx.step(
            "Update the requesting production function",
            lambda: purchasing.notify_requester(
                _t(ctx, "service.notify_requester", "workflow"),
                requirement=validated,
                message=f"Purchase order {po['po_number']} for {draft['quantity']} {validated['unit']} of {validated['description']} is placed with {draft['supplier_name']} and confirmed for {confirmation.get('confirmed_date') or draft['required_date']}.",
            ),
            component="service.notify_requester",
        )
        return "Requirement fulfilled"
    handling = ctx.step(
        f"Handle rejected orders, delays and unavailable components #{attempt}",
        lambda: run_agent(
            "agent.supplier_reply_interpretation",
            {
                "validated_requirement": validated,
                "candidate_suppliers": [
                    c for c in candidates if c["supplier_id"] not in excluded + [draft["supplier_id"]]
                ],
                "contract_conditions": conditions,
                "confirmation_status": confirmation,
                "supplier_evaluation": evaluation,
                "process_instance_id": ctx.instance_id,
                "objective": "Decide: accept a new date, split, alternative supplier, escalate or cannot fulfil",
            },
            ctx.instance_id,
        ),
        component="agent.supplier_reply_interpretation",
        kind="agent",
    )
    if handling.get("resolution") == "accept_new_date":
        ctx.step(
            "Update the requesting production function",
            lambda: purchasing.notify_requester(
                _t(ctx, "service.notify_requester", "workflow"),
                requirement=validated,
                message=f"Purchase order {po['po_number']} is delayed; the new confirmed date {confirmation.get('confirmed_date')} still meets your required date {validated['required_date']}.",
            ),
            component="service.notify_requester",
        )
        return "Requirement fulfilled"
    if handling.get("resolution") == "alternative_supplier" and attempt < 2:
        ctx.state["excluded_suppliers"] = excluded + [draft["supplier_id"]]
        ctx.counter("sourcing")
        return production_purchase(ctx)  # evaluate an alternative supplier: the same steps, next attempt number
    ctx.review(
        "Handle rejected orders, delays and unavailable components",
        f"Supplier problem ({confirmation.get('status')}): {handling.get('rationale')}",
        {"resolution": handling.get("resolution")},
    )
    return "Requirement cannot be fulfilled"
