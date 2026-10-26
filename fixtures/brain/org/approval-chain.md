---
type: org
id: approval-chain
people: []
---

# Approval chain: who releases what

AI colleagues prepare. Humans release. This is the autonomy level L1 (see `authority-matrix.md`).

| Document | Prepared by | Released by | If that person is away |
| --- | --- | --- | --- |
| Purchase order up to 250,000.00 DKK | Buyer (parked) | Mette Lindgren | Jesper Holm |
| Purchase order above 250,000.00 DKK | A human buyer | Mette Lindgren, after Søren Vestergaard agrees | Søren Vestergaard |
| Invoice that matched or was resolved | AP specialist (parked) | Mette Lindgren | Jesper Holm |
| Purchase outside any contract | A human buyer | The category manager decides first, then Mette Lindgren releases | Søren Vestergaard |

## Rules

- An AI colleague never releases. It asks with `request_approval` and says plainly who will release.
- The person who prepared a document never releases it (segregation of duties).
- A released document is final for the colleague: any change is a new request.
- The manager's backup acts only when the manager is recorded as away; the colleague asks Mette
  Lindgren first and tells her it asked the backup.
