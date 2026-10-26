---
type: process
id: contract-renewal
role: supplier_manager
kind: contract
---

# Contract renewal (as it works today)

1. **90-day reminder.** Jaggaer sends a reminder to the category manager 90 days before expiry.
2. **Decision.** The category manager decides: renew as is, renegotiate or tender.
3. **Buyers.** Buyers are told only when the new contract is entered; in between they keep buying
   at the old price until the end date.

## Where it goes wrong

Reminders are missed. A contract price expires and the next PO is entered at a list price.

## What changes with the colleagues

The `contract.expiring` event goes to the Supplier manager, who tells the colleague manager and
the buyer side with the contract, the supplier and the end date. There is no sourcing colleague
in the proof of concept, so the category manager still decides.
