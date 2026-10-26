"""Deterministic services of the supplier query workflow and the shared intake (section 3)."""

from __future__ import annotations

import re
from typing import Any

from pev2 import config
from pev2.errors import BusinessException
from pev2.services import payments
from pev2.services.common import Tools

INVOICE_NUMBER = re.compile(r"\b(\d{4}-\d{3,6}|INV\d{3,6}|\d{6})\b")


def classify_message(text: str) -> str:
    """The AI-function step; a keyword classifier stands in for the model call (the output schema is the same)."""
    low = text.lower()
    if any(w in low for w in ("paid", "payment", "pay ", "when will", "remittance")):
        return "payment_question"
    if any(w in low for w in ("credit note", "invoice")):
        return "invoice_reply"
    if any(w in low for w in ("deliver", "delivery", "new date", "late")):
        return "delivery_reply"
    return "other"


def classify_and_open_case(t: Tools, *, message: str, sender: str) -> dict[str, Any]:
    """Extract the enquiry type and the supplier invoice number, open a case owned by the Supplier Manager, and
    acknowledge receipt (a generic receipt, no invoice information)."""
    kind = classify_message(message)
    ref = INVOICE_NUMBER.search(message)
    created = t(
        "jira.create_issue",
        idempotency_key=f"{t.instance_id}:case",
        project="SUP",
        summary=f"Payment question from {sender}",
        description=message,
        issue_type="supplier_enquiry",
        assignee="Supplier Manager",
        instance_id=t.instance_id,
    )
    ack = t(
        "email.create_draft",
        idempotency_key=f"{t.instance_id}:ack-draft",
        to=[sender],
        subject="We received your message",
        body="Thank you, we have received your message and will come back to you.",
        thread_id=f"case:{created['issue_key']}",
        instance_id=t.instance_id,
    )
    t(
        "email.send_approved",
        idempotency_key=f"{t.instance_id}:ack-send",
        draft_id=ack["draft_id"],
        policy="payment-answer",
    )
    return {
        "case_key": created["issue_key"],
        "enquiry": {
            "type": kind,
            "supplier_invoice_number": ref.group(1) if ref else None,
            "message": message,
            "sender": sender,
        },
    }


def fetch_payment_playbook(t: Tools) -> dict[str, Any]:
    pb = t("brain.fetch_playbook", playbook="payment-question")
    return {"playbook": pb["playbook"], "version": pb["version"]}


def identify_supplier_from_sender(t: Tools, *, sender: str) -> dict[str, Any]:
    """The sender must be a known contact of exactly one supplier; otherwise the enquiry is handed off."""
    try:
        sup = t("sap.get_supplier", email=sender)
    except BusinessException as exc:
        raise BusinessException("unknown_sender", f"{sender} is not a known supplier contact.") from exc
    return {"supplier_id": sup["id"], "supplier_name": sup["name"]}


def hand_off_to_ap(t: Tools, *, case_key: str, reason: str, facts: dict[str, Any]) -> dict[str, Any]:
    """Assign the case internally with the facts and the reason automated answering stopped; never present an
    unverified payment date."""
    t(
        "jira.assign_issue",
        idempotency_key=f"{t.instance_id}:handoff-assign",
        issue_key=case_key,
        assignee="AP Specialist",
    )
    t(
        "jira.add_comment",
        idempotency_key=f"{t.instance_id}:handoff-comment",
        issue_key=case_key,
        body=f"Handed to Accounts Payable: {reason}. Facts: {facts}",
    )
    return {"handover_note": reason}


def compute_dates(t: Tools, *, invoice_facts: dict[str, Any], terms: dict[str, Any]) -> dict[str, Any]:
    return {"dates": payments.compute(invoice_facts, terms, config.today())}


def draft_payment_answer(
    t: Tools, *, invoice_facts: dict[str, Any], dates: dict[str, str], supplier_invoice_number: str
) -> dict[str, Any]:
    text = payments.draft_payment_answer(invoice_facts, dates, supplier_invoice_number)
    assert "expectation, not a promise" in text
    return {"payment_answer": text}


def send_answer_and_record(t: Tools, *, case_key: str, sender: str, answer: str) -> dict[str, Any]:
    """Send under the payment-answer policy, record on the case, mark answered only after delivery."""
    draft = t(
        "email.create_draft",
        idempotency_key=f"{t.instance_id}:answer-draft",
        to=[sender],
        subject="Your payment question",
        body=answer,
        thread_id=f"case:{case_key}",
        instance_id=t.instance_id,
    )
    sent = t(
        "email.send_approved",
        idempotency_key=f"{t.instance_id}:answer-send",
        draft_id=draft["draft_id"],
        policy="payment-answer",
    )
    t(
        "jira.add_comment",
        idempotency_key=f"{t.instance_id}:answer-comment",
        issue_key=case_key,
        body=f"Answered: {answer}",
    )
    t("jira.update_issue", idempotency_key=f"{t.instance_id}:answer-close", issue_key=case_key, transition="answered")
    return {"sent_answer": sent}
