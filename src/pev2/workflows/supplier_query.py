"""Supplier query workflow (section 4): a supplier asks when it gets paid. Read-only on SAP; dates in code; the answer
states an expectation, not a promise."""

from __future__ import annotations

from typing import Any

from pev2.engine import Context, workflow
from pev2.errors import BusinessException
from pev2.gates import invoice_match_and_posting
from pev2.services import enquiries
from pev2.services.common import Tools


def _t(ctx: Context, service: str, role: str = "supplier_manager") -> Tools:
    return Tools(caller=service, role=role, instance_id=ctx.instance_id)


@workflow(
    "supplier_query",
    title="A supplier asks when it gets paid",
    trigger="Payment enquiry received",
    end_states=["Enquiry answered", "Handed to AP Specialist"],
)
def supplier_query(ctx: Context) -> str:
    message, sender = ctx.trigger["message"], ctx.trigger["sender"]
    opened = ctx.step(
        "Classify message and open case",
        lambda: enquiries.classify_and_open_case(
            _t(ctx, "service.classify_and_open_case", "workflow"), message=message, sender=sender
        ),
        component="service.classify_and_open_case",
    )
    ctx.step(
        "Fetch payment-question playbook",
        lambda: enquiries.fetch_payment_playbook(_t(ctx, "service.fetch_payment_playbook")),
        component="service.fetch_payment_playbook",
    )
    enquiry = opened["enquiry"]

    def handoff(reason: str, facts: dict[str, Any]) -> str:
        ctx.step(
            "Hand the enquiry to the AP Specialist",
            lambda: enquiries.hand_off_to_ap(
                _t(ctx, "service.hand_off_to_ap", "workflow"), case_key=opened["case_key"], reason=reason, facts=facts
            ),
            component="service.hand_off_to_ap",
        )
        return "Handed to AP Specialist"

    try:
        supplier = ctx.step(
            "Identify supplier from sender",
            lambda: enquiries.identify_supplier_from_sender(
                _t(ctx, "service.identify_supplier_from_sender"), sender=sender
            ),
            component="service.identify_supplier_from_sender",
        )
    except BusinessException as exc:
        return handoff(exc.detail, {"sender": sender})
    if not enquiry.get("supplier_invoice_number"):
        return handoff("The message names no invoice number.", {"message": message})
    try:
        found = ctx.step(
            "Find our invoice by the supplier invoice number",
            lambda: _t(ctx, "workflow", "workflow")(
                "sap.find_invoice_by_supplier_reference",
                supplier_id=supplier["supplier_id"],
                supplier_invoice_number=enquiry["supplier_invoice_number"],
            ),
            component="tool.sap.find_invoice_by_supplier_reference",
            kind="tool",
        )
    except BusinessException as exc:
        return handoff(
            f"{exc.kind}: {exc.detail}",
            {"supplier_id": supplier["supplier_id"], "supplier_invoice_number": enquiry["supplier_invoice_number"]},
        )
    checks = invoice_match_and_posting(found)
    if (
        ctx.decision(
            "Invoice found and posted?",
            "yes" if all(ok for ok, _ in checks) else "blocked / not found",
            "exactly one invoice matches and its status is posted",
        )
        != "yes"
    ):
        return handoff(
            f"Invoice {found['document_number']} is {found['status']}, not posted.",
            {"document_number": found["document_number"], "status": found["status"]},
        )
    terms = ctx.step(
        "Retrieve posting date, amount and payment terms",
        lambda: _t(ctx, "workflow", "workflow")("sap.get_payment_terms", document_number=found["document_number"]),
        component="tool.sap.get_payment_terms",
        kind="tool",
    )
    dates = ctx.step(
        "Compute due date and expected payment run",
        lambda: enquiries.compute_dates(_t(ctx, "service.compute_dates"), invoice_facts=found, terms=terms),
        component="service.compute_dates",
    )["dates"]
    answer = ctx.step(
        "Draft answer to the supplier",
        lambda: enquiries.draft_payment_answer(
            _t(ctx, "service.draft_payment_answer"),
            invoice_facts=found,
            dates=dates,
            supplier_invoice_number=enquiry["supplier_invoice_number"],
        ),
        component="service.draft_payment_answer",
    )["payment_answer"]
    ctx.step(
        "Send answer and record on case",
        lambda: enquiries.send_answer_and_record(
            _t(ctx, "service.send_answer_and_record", "workflow"),
            case_key=opened["case_key"],
            sender=sender,
            answer=answer,
        ),
        component="service.send_answer_and_record",
    )
    return "Enquiry answered"
