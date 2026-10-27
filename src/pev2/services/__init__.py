"""Deterministic services and their tool allow-lists (section 3). A service may only call what is listed here."""

from pev2.tools import registry

ALLOW: dict[str, list[str]] = {
    "service.open_case": ["jira.create_issue"],  # the uncovered-case workflow: a case for a person, nothing else
    "service.open_case_and_assign_owner": ["jira.get_issue", "jira.create_issue", "jira.assign_issue"],
    "service.receive_invoice": [
        "sap.get_invoice",
        "email.receive_event",
        "email.get_attachments",
        "dms.get_document",
        "dms.store_document",
    ],
    "service.extract_invoice_details": ["sap.get_invoice", "email.get_attachments"],
    "service.identify_supplier": ["sap.get_supplier"],
    "service.locate_purchase_order": ["sap.get_purchase_order", "sap.find_purchase_orders", "sap.get_goods_receipt"],
    "service.retrieve_contract": ["sap.get_contract"],
    "service.validate_invoice": ["sap.get_invoice", "sap.get_contract"],
    "service.fetch_ap_playbook": ["brain.fetch_playbook"],
    "service.match_invoice": [
        "sap.get_invoice",
        "sap.find_invoice_by_supplier_reference",
        "sap.get_purchase_order",
        "sap.get_goods_receipt",
        "sap.get_contract",
        "sap.get_supplier",
    ],
    "service.retrieve_supplier_history": ["brain.supplier_brief", "sap.get_contract"],
    "service.block_invoice": ["sap.get_invoice", "sap.block_invoice"],
    "service.attach_reply": ["jira.get_issue", "jira.add_comment"],
    "service.rematch": ["sap.get_invoice", "sap.get_purchase_order", "sap.get_goods_receipt", "sap.get_contract"],
    "service.park_invoice": ["sap.park_invoice", "sap.get_invoice"],
    "service.release_invoice": ["sap.release_invoice", "sap.get_invoice"],
    "service.prepare_notification": ["email.create_draft", "jira.create_issue", "jira.add_comment"],
    "service.submit_invoice": ["sap.submit_invoice", "sap.get_invoice", "sap.get_supplier"],
    "service.receive_requirement": [],
    "service.validate_component": ["sap.get_material"],
    "service.check_coverage": ["sap.get_stock", "sap.find_purchase_orders", "sap.get_purchase_order"],
    "service.identify_approved_suppliers": ["sap.get_supplier", "sap.get_material"],
    "service.retrieve_contract_conditions": ["sap.get_contract"],
    "service.select_supplier": [],
    "service.check_authorization": [],
    "service.submit_purchase_order": ["sap.create_purchase_order", "sap.get_supplier"],
    "service.track_confirmation": ["sap.get_supplier_confirmation", "email.track_correspondence"],
    "service.notify_requester": ["email.create_draft", "email.send_approved"],
    "service.classify_and_open_case": ["jira.create_issue", "email.create_draft", "email.send_approved"],
    "service.fetch_payment_playbook": ["brain.fetch_playbook"],
    "service.identify_supplier_from_sender": ["sap.get_supplier"],
    "service.hand_off_to_ap": ["jira.assign_issue", "jira.add_comment"],
    "service.compute_dates": [],
    "service.draft_payment_answer": [],
    "service.send_answer_and_record": [
        "email.create_draft",
        "email.send_approved",
        "jira.add_comment",
        "jira.update_issue",
    ],
    "workflow": [
        "sap.find_invoice_by_supplier_reference",
        "sap.get_payment_terms",
        "sap.get_contract",
        "email.track_correspondence",
        "email.send_approved",
        "email.create_draft",
        "jira.add_comment",
        "jira.get_issue",
        "brain.remember_case",
        "sap.get_invoice",
    ],
}

for caller, tools in ALLOW.items():
    registry.permit(caller, tools)
