---
type: process
id: invoice-handling
role: ap_specialist
kind: invoice
---

# Invoice handling (as it works today)

1. **Scan.** Invoices arrive by e-mail or EDI and are scanned into SAP in the status received.
2. **Match.** An AP clerk compares the invoice with the PO and the goods receipt, line by line.
3. **Exception queue.** Anything that does not match goes to the exception queue.
4. **Deviation e-mail.** The clerk writes to the supplier (price) or the buyer (quantity) and
   waits. Typical turnaround is five working days.
5. **Post and pay.** A matched invoice is posted by a manager and paid in the next payment run.

## Where it goes wrong

- Price deviations sit in the queue for days because nobody remembers the contract price.
- Quantity deviations are often a goods receipt that was posted late.
- Invoices without a PO are the slowest: the supplier is chased by phone.

## What changes with the colleagues

The AP specialist matches the moment the invoice arrives, writes to the supplier the same hour
with the contract price in the message, follows up after two days, and parks the invoice for
Mette Lindgren once the facts agree.
