"""Invoice processing workflow (section 4): receive, extract, identify, PO and contract in parallel, compare, validate,
discrepancy analysis by an agent, approval or supplier clarification, submission to the financial workflow."""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from typing import Any

from pev2.engine import Context, workflow
from pev2.errors import BusinessException
from pev2.gates import discrepancy_route, invoice_valid, resolved
from pev2.gateway import run_agent
from pev2.services import invoices
from pev2.services.common import Tools, to_eur
from pev2.tools import limits


def _t(ctx: Context, service: str, role: str = "ap_specialist") -> Tools:
    return Tools(caller=service, role=role, instance_id=ctx.instance_id)


@workflow(
    "invoice_processing",
    title="Procurement invoice processing",
    trigger="Invoice received",
    end_states=["Processing completed", "Escalated"],
)
def invoice_processing(ctx: Context) -> str:
    invoice_id = ctx.trigger["invoice_id"]
    sender = ctx.trigger.get("sender", "edi")
    received = ctx.step(
        "Receive invoice",
        lambda: invoices.receive_invoice(
            _t(ctx, "service.receive_invoice", "workflow"), invoice_id=invoice_id, sender=sender
        ),
        component="service.receive_invoice",
    )
    case = ctx.step(
        "Open case",
        lambda: invoices.open_case_and_assign(
            _t(ctx, "service.open_case_and_assign_owner", "workflow"),
            subject=f"Invoice {invoice_id}",
            kind="invoice",
            owner="AP Specialist",
        ),
        component="service.open_case_and_assign_owner",
    )
    try:
        data = ctx.step(
            "Extract invoice details",
            lambda: invoices.extract_invoice_details(
                _t(ctx, "service.extract_invoice_details"), invoice_id=received["invoice_reference"]
            ),
            component="service.extract_invoice_details",
        )
    except BusinessException as exc:
        ctx.review("Extract invoice details", f"{exc.kind}: {exc.detail}", exc.data)
        return "Escalated"
    ctx.gate(
        "Extract invoice details: confidence & schema gate",
        [
            (Decimal(data["confidence"]) >= Decimal("0.6"), "extraction confidence too low"),
            (
                data["lines_total"] == str(Decimal(data["amount"]).quantize(Decimal("0.01"))),
                "totals do not equal the sum of lines",
            ),
        ],
    )

    exceptions: list[str] = []
    supplier = None
    try:
        supplier = ctx.step(
            "Identify supplier",
            lambda: invoices.identify_supplier(_t(ctx, "service.identify_supplier"), invoice_data=data),
            component="service.identify_supplier",
        )
    except BusinessException as exc:
        exceptions.append(f"{exc.kind}: {exc.detail}")  # modelled exception path → Identify discrepancies
    located, contract = None, None
    if supplier:
        materials = [ln["material_id"] for ln in data["lines"]]

        def _po() -> dict[str, Any]:
            try:
                return invoices.locate_purchase_order(
                    _t(ctx, "service.locate_purchase_order"), invoice_data=data, supplier_id=supplier["supplier_id"]
                )
            except BusinessException as exc:
                return {"exception": f"{exc.kind}: {exc.detail}"}

        def _contract() -> dict[str, Any]:
            try:
                return invoices.retrieve_contract(
                    _t(ctx, "service.retrieve_contract"), supplier_id=supplier["supplier_id"], materials=materials
                )
            except BusinessException as exc:
                return {"exception": f"{exc.kind}: {exc.detail}"}

        par = ctx.parallel(
            "parallel_po_and_contract",
            [
                ("Retrieve supplier contract", _contract, "tool.sap.get_contract"),
                ("Locate related purchase order", _po, "service.locate_purchase_order"),
            ],
        )
        contract = None if "exception" in par["Retrieve supplier contract"] else par["Retrieve supplier contract"]
        located = None if "exception" in par["Locate related purchase order"] else par["Locate related purchase order"]
        exceptions += [p["exception"] for p in par.values() if "exception" in p]

    validation = {
        "status": "invalid",
        "issues": exceptions,
        "deviation": "0",
        "deviation_eur": "0",
        "requires_approval": True,
    }
    comparison: dict[str, Any] = {"match": {"differences": []}}
    if supplier and located:
        try:
            comparison = ctx.step(
                "Compare invoice with PO and contract",
                lambda: invoices.compare_invoice_po_contract(data, located, contract),
                component="service.compare",
            )
            validation = ctx.step(
                "Validate prices, quantities, currency, tax and tolerances",
                lambda: invoices.validate_invoice(
                    _t(ctx, "service.validate_invoice"), invoice_data=data, comparison=comparison
                ),
                component="service.validate_invoice",
            )
        except BusinessException as exc:
            if exc.kind == "duplicate_invoice":
                ctx.review(
                    "Validate prices, quantities, currency, tax and tolerances", f"{exc.kind}: {exc.detail}", exc.data
                )
                return "Escalated"
            exceptions.append(f"{exc.kind}: {exc.detail}")
            validation = {**validation, "issues": exceptions}
    ctx.gate("Invoice valid?", invoice_valid(validation))
    approval_ref = None
    if (
        ctx.decision(
            "Invoice valid?", "valid" if validation["status"] == "valid" else "invalid", "all validation rules passed"
        )
        == "invalid"
    ):
        analysis = ctx.step(
            "Identify discrepancies",
            lambda: run_agent(
                "agent.discrepancy_analysis",
                {
                    "validation_result": validation,
                    "comparison": comparison,
                    "invoice_data": {
                        k: data[k] for k in ("invoice_id", "supplier_id", "invoice_number", "amount", "currency")
                    },
                    "exceptions": exceptions,
                    "process_instance_id": ctx.instance_id,
                    "objective": "Classify and explain each discrepancy with its likely cause",
                },
                ctx.instance_id,
            ),
            component="agent.discrepancy_analysis",
            kind="agent",
        )
        deviation_eur = to_eur(Decimal(str(validation.get("deviation") or "0")), data["currency"])
        owner = "Procurement Manager" if deviation_eur > limits.DISCREPANCY_LEAD_LIMIT_EUR else "AP Team Lead"
        ctx.gate("Discrepancy resolution route", discrepancy_route(deviation_eur, owner, analysis["cause"]))
        route = ctx.decision(
            "Discrepancy resolution route",
            "internal approval" if analysis["cause"] == "internal" else "supplier clarification",
            "internal cause → approval; external cause → supplier clarification",
        )
        if route == "internal approval":
            approval = ctx.approval(
                "Approve discrepancy",
                owner,
                {
                    "invoice_id": invoice_id,
                    "deviation_eur": str(deviation_eur),
                    "explanations": analysis["explanations"],
                },
                timeout_days=3,
                escalation="Procurement Manager",
            )
            if approval["decision"] != "approved":
                return "Escalated"
            approval_ref = approval["id"]
        else:
            if not supplier:
                ctx.review(
                    "Create supplier notification or dispute case",
                    "The supplier could not be identified; no clarification can be sent.",
                    {"exceptions": exceptions},
                )
                return "Escalated"
            prepared = ctx.step(
                "Create supplier notification or dispute case",
                lambda: invoices.prepare_notification_or_dispute(
                    _t(ctx, "service.prepare_notification", "workflow"),
                    discrepancy=analysis,
                    invoice_data=data,
                    supplier=supplier,
                    case_key=case["case_key"],
                ),
                component="service.prepare_notification",
            )
            send_ok = ctx.approval(
                "Approve outbound supplier communication",
                "AP Specialist",
                {
                    "draft_id": prepared["draft_id"],
                    "to": supplier["email"],
                    "text": prepared["body"],
                    "invoice_id": invoice_id,
                },
                timeout_days=2,
                escalation="AP Team Lead",
            )
            if send_ok["decision"] != "approved":
                return "Escalated"
            ctx.step(
                "Send supplier notification",
                lambda: _t(ctx, "workflow", "workflow")(
                    "email.send_approved",
                    idempotency_key=ctx.idem("send-notify"),
                    approval_ref=send_ok["id"],
                    draft_id=prepared["draft_id"],
                    approval_reference=send_ok["id"],
                ),
                component="tool.email.send_approved",
                kind="tool",
            )
            reply = ctx.wait_event(
                "Wait for: Supplier response received",
                "supplier_reply",
                prepared["thread_id"],
                timeout=timedelta(days=10),
            )
            tracked = ctx.step(
                "Track supplier response",
                lambda: _t(ctx, "workflow", "workflow")("email.track_correspondence", thread_id=prepared["thread_id"]),
                component="tool.email.track_correspondence",
                kind="tool",
            )
            reval = ctx.step(
                "Revalidate corrected information",
                lambda: run_agent(
                    "agent.discrepancy_analysis",
                    {
                        "supplier_reply": reply.get("body", ""),
                        "timed_out": reply.get("timed_out", False),
                        "validation_result": validation,
                        "replies": tracked["replies"],
                        "process_instance_id": ctx.instance_id,
                        "objective": "Interpret the supplier reply: corrected, unresolved or needs information",
                    },
                    ctx.instance_id,
                ),
                component="agent.discrepancy_analysis",
                kind="agent",
            )
            ctx.gate(
                "Resolved?",
                resolved(
                    {
                        "supplier_decision": "accept"
                        if reval["revalidation"] == "corrected"
                        else (
                            "needs_information"
                            if reval["revalidation"] == "needs_information"
                            else ("no_reply" if reply.get("timed_out") else "dispute")
                        )
                    }
                ),
            )
            if (
                ctx.decision(
                    "Resolved?",
                    "complete" if reval["revalidation"] == "corrected" else "escalate",
                    "approved / corrected vs rejected / no response",
                )
                == "escalate"
            ):
                return "Escalated"
            approval = ctx.approval(
                "Approve discrepancy",
                owner,
                {
                    "invoice_id": invoice_id,
                    "deviation_eur": str(deviation_eur),
                    "explanations": analysis["explanations"],
                    "supplier_reply": reply.get("body", ""),
                },
                timeout_days=3,
                escalation="Procurement Manager",
            )
            if approval["decision"] != "approved":
                return "Escalated"
            approval_ref = approval["id"]
    ctx.gate(
        "Pre-check: Submit to financial workflow",
        [
            (
                validation["status"] == "valid" or approval_ref is not None,
                "only a valid or approved invoice may be posted",
            )
        ],
    )
    ctx.step(
        "Submit to financial workflow",
        lambda: invoices.submit_to_financial_workflow(
            _t(ctx, "service.submit_invoice"), invoice_id=invoice_id, validation=validation, approval_ref=approval_ref
        ),
        component="service.submit_invoice",
    )
    return "Processing completed"
