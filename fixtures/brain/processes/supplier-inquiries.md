---
type: process
id: supplier-inquiries
role: supplier_manager
kind: supplier_inquiry
---

# Supplier inquiries (as it works today)

Suppliers write to the shared mailbox procurement@dc-procurement.example and to whoever they know.

## Who answers what

- **Where is my payment?** AP clerk, from SAP. Often a day or two late.
- **Where is my order, can you confirm the PO?** The buyer who placed it.
- **New prices, contract questions, onboarding?** The category manager.
- **Changed e-mail or phone for our account?** Whoever sees it first updates SAP.

## What changes with the colleagues

The Concierge reads every message. A supplier asking about a payment is a supplier inquiry and
goes to the Supplier manager, who answers the status question from SAP. The AP specialist only
works on invoice deviations, and a supplier replying to an open deviation case stays with the AP
specialist. Price and contract negotiations stay with the category manager.
