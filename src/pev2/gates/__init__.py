"""Validation gates (section 5): deterministic checks before a branch or a financial write. Each returns
(ok, reason) pairs for `Context.gate`; a failed gate parks the instance for human review."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from pev2.services.common import to_eur
from pev2.tools import limits


def invoice_valid(validation: dict[str, Any]) -> list[tuple[bool, str]]:
    """Route only a valid invoice toward submission; invalid or incomplete results go to discrepancy handling."""
    return [(validation.get("status") in ("valid", "invalid"), "validation result is incomplete")]


def discrepancy_route(deviation_eur: Decimal, approval_owner: str, cause: str) -> list[tuple[bool, str]]:
    """Deviations above 5,000 EUR route to the Procurement Manager, at or below to the AP Team Lead; unclear
    materiality or cause needs human review."""
    return [
        (cause in ("internal", "external"), f"the discrepancy cause '{cause}' is unclear; human review"),
        (
            deviation_eur <= limits.DISCREPANCY_LEAD_LIMIT_EUR or approval_owner == "Procurement Manager",
            "deviations above 5,000 EUR need the Procurement Manager",
        ),
    ]


def resolved(reply_interpretation: dict[str, Any]) -> list[tuple[bool, str]]:
    """Only documented corrections return to validation; a reply alone does not make an invoice valid."""
    return [
        (
            reply_interpretation.get("supplier_decision") in ("accept", "dispute", "needs_information", "no_reply"),
            "the supplier reply could not be classified",
        )
    ]


def procurement_necessary(coverage: dict[str, Any]) -> list[tuple[bool, str]]:
    return [(coverage.get("covered") in (True, False), "coverage could not be established")]


def confirmation_acceptable(confirmation: dict[str, Any], requirement: dict[str, Any]) -> list[tuple[bool, str]]:
    """A confirmation is acceptable only when quantity and date meet the requirement."""
    return [
        (
            confirmation.get("status") in ("confirmed", "rejected", "delayed", "unavailable", "pending", "timeout"),
            "unknown confirmation status",
        )
    ]


def invoice_match_and_posting(found: dict[str, Any]) -> list[tuple[bool, str]]:
    """Continue to payment terms only for a unique, supplier-matched, posted invoice."""
    return [(found.get("status") == "posted", f"invoice is {found.get('status')}, not posted")]


def pre_block(match: dict[str, Any]) -> list[tuple[bool, str]]:
    return [
        (not match["within_tolerance"], "the invoice is within tolerance; blocking would be wrong"),
        (bool(match.get("blocking")), "no blocking difference"),
    ]


def pre_park(match: dict[str, Any], rematch: dict[str, Any] | None) -> list[tuple[bool, str]]:
    return [
        (
            bool(match["within_tolerance"]) or bool(rematch and rematch.get("ok")),
            "neither within policy nor covered by a confirmed credit note",
        )
    ]


def pre_release(approval: dict[str, Any], invoice_status: str) -> list[tuple[bool, str]]:
    return [
        (approval.get("decision") == "approved", "no release approval"),
        (invoice_status == "parked", f"invoice is {invoice_status}, not parked"),
    ]


def pre_submit_po(
    draft: dict[str, Any], authorization: dict[str, Any], approval: dict[str, Any] | None
) -> list[tuple[bool, str]]:
    needs = authorization["requires_approval"]
    return [
        (authorization["bound_to"]["version"] == draft["version"], "the draft changed after authorization"),
        (
            not needs or (approval is not None and approval.get("decision") == "approved"),
            "the Procurement Manager has not approved this order",
        ),
        (
            to_eur(Decimal(draft["total"]), draft["currency"]) == Decimal(draft["total_eur"]),
            "EUR amount does not match",
        ),
    ]
