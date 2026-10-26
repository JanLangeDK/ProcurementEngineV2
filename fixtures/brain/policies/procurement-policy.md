---
type: policy
id: procurement-policy
applies_to: [requisition-to-po, contract-renewal, escalation]
---

# Procurement policy

## Contract first

Buy under a valid contract price whenever one exists. A price is valid when the delivery date
falls inside its validity dates and the supplier is not blocked. The Buyer never invents a price.

## No valid contract price

A request for a material with no valid contract price goes to the category manager when the
value is above 25,000 DKK. When the value is not known, treat it as above 25,000 DKK. Below
25,000 DKK a human buyer decides, and the category manager is informed. Either way the AI Buyer
does not create the PO.

## Quotes

Without a contract, purchases above 100,000 DKK need three written quotes before an order.

## No PO, no pay

An invoice without a PO is not paid. The AP specialist asks the supplier for the PO number.

## Blocked suppliers

A blocked supplier receives no new PO and no payment until the block is lifted by the category
manager. A request for a blocked supplier is handed back with that reason.

## Contract expiry

The category manager is told 90 days before a contract ends and decides to renew, renegotiate
or tender. Until then the contract price stays valid until its end date and not a day longer.
