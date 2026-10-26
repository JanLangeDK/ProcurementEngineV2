"""The authority matrix as code (section 6). Enforced by the tool registry on every write, never in a prompt.

- Financial writes that section 6 binds to a human decision need an `approval_ref` of a decided, approving
  approval whose owner matches: release_invoice → AP Team Lead; create_purchase_order above 25,000 → Procurement
  Manager; submit_invoice after a discrepancy → the discrepancy approval.
- Outbound mail (`email.send_approved`) needs an approval ref, or a policy that allows it (payment answers).
- Only the roles in ROLES may call a write tool at all.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from pev2 import store
from pev2.errors import LimitError

PO_APPROVAL_LIMIT_EUR = Decimal("25000.00")
DISCREPANCY_LEAD_LIMIT_EUR = Decimal("5000.00")

# write tool -> roles (workflow components act as these roles; agents never hold a write role)
ROLES: dict[str, frozenset[str]] = {
    "sap.block_invoice": frozenset({"ap_specialist"}),
    "sap.park_invoice": frozenset({"ap_specialist"}),
    "sap.release_invoice": frozenset({"ap_team_lead"}),
    "sap.submit_invoice": frozenset({"ap_specialist"}),
    "sap.create_purchase_order": frozenset({"buyer"}),
    "sap.update_po_delivery_date": frozenset({"supplier_manager"}),
    "email.create_draft": frozenset({"workflow", "ap_specialist", "supplier_manager", "buyer", "agent"}),
    "email.send_approved": frozenset({"workflow"}),
    "jira.create_issue": frozenset({"workflow", "ap_specialist", "supplier_manager", "buyer", "agent"}),
    "jira.assign_issue": frozenset({"workflow", "ap_specialist", "supplier_manager", "buyer"}),
    "jira.add_comment": frozenset({"workflow", "ap_specialist", "supplier_manager", "buyer", "agent"}),
    "jira.update_issue": frozenset({"workflow", "ap_specialist", "supplier_manager", "buyer"}),
    "dms.store_document": frozenset({"workflow", "ap_specialist"}),
    "brain.remember_case": frozenset({"workflow", "supplier_manager", "ap_specialist", "buyer"}),
}
APPROVAL_OWNER: dict[str, str] = {
    "sap.release_invoice": "AP Team Lead",
    "sap.create_purchase_order": "Procurement Manager",
}
POLICY_SEND = {"payment-answer"}  # outbound messages a policy allows without a human decision


def approval_decided(ref: str | None, owner_contains: str | None = None) -> bool:
    if not ref:
        return False
    row = store.one("select * from wf_approval where id = ?", (ref,))
    if row is None or row["status"] != "approved":
        return False
    return owner_contains is None or owner_contains.lower() in str(row["owner"]).lower()


def check_write(tool_id: str, args: dict[str, Any], *, role: str | None, approval_ref: str | None) -> None:
    roles = ROLES.get(tool_id)
    if roles is not None and (role is None or role not in roles):
        raise LimitError(
            f"Segregation of duties: the {role or 'anonymous'} role may not {tool_id}; that belongs to {', '.join(sorted(roles))}."
        )
    if tool_id == "sap.release_invoice":
        if not approval_decided(approval_ref, APPROVAL_OWNER[tool_id]):
            raise LimitError(
                "An invoice is released only with a decided 'Release invoice' approval by the AP Team Lead."
            )
    if tool_id == "sap.create_purchase_order":
        total = Decimal(str(args.get("total_eur") or args.get("total") or "0"))
        if total > PO_APPROVAL_LIMIT_EUR and not approval_decided(approval_ref, APPROVAL_OWNER[tool_id]):
            raise LimitError(
                f"Purchase orders above {PO_APPROVAL_LIMIT_EUR:,.2f} EUR need the Procurement Manager's approval; this one is {total:,.2f} EUR."
            )
        if not args.get("authorization_ok"):
            raise LimitError("Only authorized orders may be submitted: the authorization check must pass first.")
    if tool_id == "sap.submit_invoice":
        if args.get("status") != "valid":
            raise LimitError("Only invoices with status valid may be posted.")
        if args.get("requires_discrepancy_approval") and not approval_decided(approval_ref):
            raise LimitError("A discrepancy on this invoice needs a decided approval before it is posted.")
    if tool_id == "email.send_approved":
        policy = args.get("policy")
        if policy not in POLICY_SEND and not approval_decided(approval_ref):
            raise LimitError(
                "External communication is sent only after a human approval (or under an explicit sending policy)."
            )
