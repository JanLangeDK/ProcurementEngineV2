---
type: process
id: late-delivery
role: supplier_manager
kind: delivery
---

# Late delivery (as it works today)

1. **Who notices.** The planner at the plant, when the goods are not on the dock.
2. **Who calls.** The planner phones the supplier, or asks the buyer to. There is no log.
3. **Who tells the plant.** Nobody systematically; the planner passes it on.
4. **Aftermath.** The late delivery is remembered only by the people involved. A contract's
   penalty clause is rarely looked up.

## Who is told when a delivery slips

- The requester who placed the order.

## What changes with the colleagues

The `delivery.slipped` event wakes the Supplier manager before anyone asks. It checks the PO and
the goods receipts, reads the supplier's history and the contract's delivery and penalty terms,
asks the supplier for a new date, tells everyone listed above, and follows up. The case is
remembered against the supplier so the next buyer sees it.
