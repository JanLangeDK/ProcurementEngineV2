---
type: policy
id: authority-matrix
applies_to: [requisition-to-po, invoice-handling, escalation]
---

# Authority matrix

What each role may do in the systems. The systems enforce this table; it is not a request.
`systems/limits.py` holds the same table and a test keeps them equal.

| role | tool | cap_dkk |
| --- | --- | --- |
| buyer | park_po | 250000.00 |
| manager | release_po | - |
| ap_specialist | park_invoice | - |
| ap_specialist | block_invoice | - |
| ap_specialist | add_invoice_note | - |
| manager | release_invoice | - |
| supplier_manager | update_supplier_contact | - |
| manager | post_goods_receipt | - |

Posting a goods receipt is a warehouse action: in the demo the simulator does it with the manager token, and no AI colleague may.

Everyone may read SAP and Jaggaer. The Concierge may not write anything.

## Segregation of duties

The Buyer cannot release a PO or touch an invoice. The AP specialist cannot create or release a
PO and cannot post an invoice. The Supplier manager cannot write any document. A refusal from the
system is final: read the sentence, tell the right person, do not try another tool.

## Autonomy levels

- **L0** observes and answers questions.
- **L1** prepares work and parks it; a human releases. This is the level in the demo.
- **L2** acts within limits and a human reviews afterwards.
- **L3** acts and handles exceptions on its own.

Moving a colleague up a level is a decision for the colleague manager and the CPO, made by
changing the matrix and the limits together.
