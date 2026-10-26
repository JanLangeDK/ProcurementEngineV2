"""Payment date arithmetic: pure and tested, the only place a payment date is computed (section 3)."""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]


def weekday_index(name: str) -> int:
    try:
        return WEEKDAYS.index(name.strip().lower())
    except ValueError:
        raise ValueError(f"unknown weekday {name!r}") from None


def due_date(baseline: date, terms_days: int) -> date:
    """Due date = baseline date + payment terms in days."""
    return baseline + timedelta(days=terms_days)


def expected_payment_run(due: date, today: date, run_weekday: str = "thursday") -> date:
    """The first payment run on or after the due date, never in the past."""
    start = max(due, today)
    return start + timedelta(days=(weekday_index(run_weekday) - start.weekday()) % 7)


def compute(invoice_facts: dict[str, Any], terms: dict[str, Any], today: date) -> dict[str, str]:
    baseline = date.fromisoformat(str(terms.get("baseline_date") or invoice_facts.get("posting_date"))[:10])
    due = due_date(baseline, int(terms["terms_days"]))
    run = expected_payment_run(due, today, str((terms.get("payment_run_days") or ["thursday"])[0]))
    return {
        "baseline_date": baseline.isoformat(),
        "terms_days": str(terms["terms_days"]),
        "due_date": due.isoformat(),
        "expected_payment_run": run.isoformat(),
    }


def draft_payment_answer(invoice_facts: dict[str, Any], dates: dict[str, str], supplier_invoice_number: str) -> str:
    """The answer from verified facts and code-computed dates. States an expectation, never a promise."""
    due = date.fromisoformat(dates["due_date"])
    run = date.fromisoformat(dates["expected_payment_run"])
    return (
        f"Invoice {supplier_invoice_number} (our reference {invoice_facts['document_number']}) for "
        f"{invoice_facts['currency']} {invoice_facts['amount']} is posted, with {dates['terms_days']}-day terms, "
        f"due on {due.strftime('%-d %B')}. The expected payment run is {run.strftime('%A %-d %B')}. "
        "That is an expectation, not a promise."
    )
