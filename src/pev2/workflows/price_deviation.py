"""Price deviation workflow (section 4): INV7731 arrives, is matched in code, blocked, a credit note requested by an
agent, the reply interpreted by an agent, re-matched, parked, and released by the AP Team Lead."""

from __future__ import annotations

from datetime import timedelta

from pev2.engine import Context, workflow
from pev2.errors import BusinessException
from pev2.gates import pre_block, pre_park, pre_release
from pev2.gateway import run_agent
from pev2.services import invoices
from pev2.services.common import Tools


def _t(ctx: Context, service: str, role: str = "ap_specialist") -> Tools:
    return Tools(caller=service, role=role, instance_id=ctx.instance_id)


@workflow(
    "price_deviation",
    title="Invoice with a price deviation",
    trigger="Invoice received",
    end_states=["Invoice released for payment", "Escalated to AP Team Lead"],
)
def price_deviation(ctx: Context) -> str:
    invoice_id = ctx.trigger["invoice_id"]
    case = ctx.step(
        "Open case and assign owner",
        lambda: invoices.open_case_and_assign(
            _t(ctx, "service.open_case_and_assign_owner", "workflow"),
            subject=f"Invoice {invoice_id}",
            kind="invoice",
            owner="AP Specialist",
            description=f"Invoice {invoice_id} received from {ctx.trigger.get('sender', 'EDI')}",
        ),
        component="service.open_case_and_assign_owner",
    )
    policy = ctx.step(
        "Fetch AP playbook and tolerance policy",
        lambda: invoices.fetch_tolerance_policy(_t(ctx, "service.fetch_ap_playbook")),
        component="service.fetch_ap_playbook",
    )
    try:
        match = ctx.step(
            "Match invoice against PO, goods receipts and contract prices",
            lambda: invoices.match_invoice(_t(ctx, "service.match_invoice"), invoice_id=invoice_id, policy=policy),
            component="service.match_invoice",
        )
    except BusinessException as exc:
        ctx.review(
            "Match invoice against PO, goods receipts and contract prices", f"{exc.kind}: {exc.detail}", exc.data
        )
        return "Escalated to AP Team Lead"

    rematch = None
    if (
        ctx.decision(
            "Within tolerance?",
            "yes" if match["within_tolerance"] else "no",
            "all line differences within the policy tolerance",
        )
        == "no"
    ):
        ctx.step(
            "Retrieve supplier history and contract terms",
            lambda: invoices.retrieve_supplier_history(
                _t(ctx, "service.retrieve_supplier_history"), supplier_id=match["invoice"]["supplier_id"]
            ),
            component="service.retrieve_supplier_history",
        )
        ctx.gate("Pre-check: Block invoice for payment", pre_block(match))
        ctx.step(
            "Block invoice for payment",
            lambda: invoices.block_invoice(_t(ctx, "service.block_invoice"), invoice_id=invoice_id, match=match),
            component="service.block_invoice",
        )
        history = ctx.state["step:Retrieve supplier history and contract terms"]
        draft = ctx.step(
            "Draft and send credit note request to supplier",
            lambda: run_agent(
                "agent.credit_note_request",
                {
                    "match_result": match,
                    "supplier_context": history["supplier_context"],
                    "process_instance_id": ctx.instance_id,
                    "objective": "Ask the supplier for a credit note for the price difference",
                },
                ctx.instance_id,
            ),
            component="agent.credit_note_request",
            kind="agent",
        )
        approval = ctx.approval(
            "Approve outbound supplier communication",
            "AP Specialist",
            {
                "draft_id": draft["draft_id"],
                "to": match["invoice"]["supplier_email"],
                "text": draft["credit_note_request"],
                "invoice_id": invoice_id,
            },
            timeout_days=2,
            escalation="AP Team Lead",
        )
        if approval["decision"] != "approved":
            ctx.step(
                "Record rejected communication",
                lambda: _t(ctx, "workflow", "workflow")(
                    "jira.add_comment",
                    idempotency_key=ctx.idem("reject-comment"),
                    issue_key=case["case_key"],
                    body="The drafted supplier message was rejected; escalated.",
                ),
                component="workflow",
                kind="tool",
            )
            return "Escalated to AP Team Lead"
        ctx.step(
            "Send approved credit note request",
            lambda: _t(ctx, "workflow", "workflow")(
                "email.send_approved",
                idempotency_key=ctx.idem("send-cn"),
                approval_ref=approval["id"],
                draft_id=draft["draft_id"],
                approval_reference=approval["id"],
            ),
            component="tool.email.send_approved",
            kind="tool",
        )
        reply = ctx.wait_event(
            "Wait for: Supplier reply received", "supplier_reply", f"inv:{invoice_id}", timeout=timedelta(days=10)
        )
        attached = ctx.step(
            "Attach reply to the open case",
            lambda: invoices.attach_reply_to_case(
                _t(ctx, "service.attach_reply", "workflow"), case_key=case["case_key"], reply=reply
            ),
            component="service.attach_reply",
        )
        interpretation = ctx.step(
            "Interpret supplier reply and record credit note",
            lambda: run_agent(
                "agent.supplier_reply_interpretation",
                {
                    "updated_case": attached,
                    "timed_out": reply.get("timed_out", False),
                    "process_instance_id": ctx.instance_id,
                    "objective": "Did the supplier accept, dispute or ask for information? Extract the credit note reference.",
                },
                ctx.instance_id,
            ),
            component="agent.supplier_reply_interpretation",
            kind="agent",
        )
        rematch = ctx.step(
            "Re-match invoice with credit note",
            lambda: invoices.rematch_with_credit_note(
                _t(ctx, "service.rematch"),
                invoice_id=invoice_id,
                match=match,
                credit_note_ref=interpretation.get("credit_note_reference"),
                supplier_decision=interpretation["supplier_decision"],
            ),
            component="service.rematch",
        )
        if (
            ctx.decision(
                "Only the confirmed difference remains?",
                "yes" if rematch.get("ok") else "no",
                "re-match shows no other deviation and the credit note is confirmed",
            )
            == "no"
        ):
            ctx.step(
                "Record escalation",
                lambda: _t(ctx, "workflow", "workflow")(
                    "jira.add_comment",
                    idempotency_key=ctx.idem("escalate-comment"),
                    issue_key=case["case_key"],
                    body=f"Escalated to the AP Team Lead: {rematch.get('reason')}",
                ),
                component="workflow",
                kind="tool",
            )
            return "Escalated to AP Team Lead"

    ctx.gate("Pre-check: Park invoice for release", pre_park(match, rematch))
    ctx.step(
        "Park invoice for release",
        lambda: invoices.park_invoice(
            _t(ctx, "service.park_invoice"), invoice_id=invoice_id, match=match, rematch=rematch
        ),
        component="service.park_invoice",
    )
    release = ctx.approval(
        "Release invoice",
        "AP Team Lead",
        {
            "invoice_id": invoice_id,
            "amount": match["invoice"]["amount"],
            "currency": match["invoice"]["currency"],
            "note": (
                "matched within tolerance"
                if match["within_tolerance"]
                else f"credit note {rematch['credit_note_ref']} confirmed"
                if rematch
                else ""
            ),
        },
        timeout_days=2,
        escalation="Finance Manager",
    )
    if release["decision"] != "approved":
        return "Escalated to AP Team Lead"
    status = ctx.step(
        "Recheck invoice state",
        lambda: _t(ctx, "workflow", "workflow")("sap.get_invoice", invoice_id=invoice_id)["status"],
        component="workflow",
        kind="tool",
    )
    ctx.gate("Pre-check: Release invoice for payment", pre_release(release, status))
    ctx.step(
        "Release invoice for payment",
        lambda: invoices.release_invoice(
            _t(ctx, "service.release_invoice", "ap_team_lead"), invoice_id=invoice_id, approval=release
        ),
        component="service.release_invoice",
    )
    ctx.step(
        "Remember case outcome",
        lambda: _t(ctx, "workflow", "workflow")(
            "brain.remember_case",
            idempotency_key=ctx.idem("remember"),
            case_id=case["case_key"],
            summary=f"Invoice {invoice_id}: {'within tolerance' if match['within_tolerance'] else 'price deviation resolved with credit note ' + str((rematch or {}).get('credit_note_ref'))}",
            entities=[match["invoice"]["supplier_id"], invoice_id],
        ),
        component="workflow",
        kind="tool",
    )
    return "Invoice released for payment"
