---
type: process
id: requisition-to-po
role: buyer
kind: requisition
---

# Requisition to PO (as it works today)

A planner needs material and the department turns the need into a purchase order.

1. **The form.** The planner fills in the requisition form on the intranet: material, quantity,
   plant, needed-by date and the coding (plant cost centre and a GL account).
2. **Two approvals.** The planner's line manager approves the budget. The category manager
   approves as well when the value is above 25,000 DKK or no contract price exists.
3. **PO entry.** A buyer enters the PO in SAP: supplier, lines, price from the info record. The
   PO is saved parked, and the colleague manager releases it.
4. **Lead time.** About three working days, almost all of it waiting for the two approvals.

## Where it goes wrong

- The wrong material is picked because two look alike (film 450 mm and 500 mm).
- The needed-by date is missing and the buyer has to phone.
- The request names a supplier who is blocked or whose contract has run out.

## What changes with the colleagues

The Buyer takes the request in chat, finds the material and the price, parks the PO and asks
Mette Lindgren to release it, in minutes. The human decisions that stay: the manager's release
and the category manager's call when there is no contract price.
