"""Invoice matching: arithmetic in code, the tolerance from the policy document (never hard-coded).

Ported from the first Procurement Engine's `matching.py` (pure, Decimal, every difference reported) and extended
with the policy application and the duplicate / missing-evidence rules of section 3.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel

from pev2.services.common import CENT, money

Kind = Literal[
    "no_po",
    "supplier_mismatch",
    "supplier_blocked",
    "currency",
    "line_not_on_po",
    "price",
    "quantity",
    "quantity_over_po",
    "not_received",
    "amount",
    "duplicate",
]


class LineIn(BaseModel):
    line_no: int
    material_id: str
    quantity: Decimal
    price: Decimal


class InvoiceIn(BaseModel):
    id: str
    supplier_id: str
    po_id: str | None = None
    currency: str = "DKK"
    amount: Decimal
    lines: list[LineIn]


class PoIn(BaseModel):
    id: str
    supplier_id: str
    lines: list[LineIn]


class ReceiptIn(BaseModel):
    po_id: str
    line_no: int
    quantity: Decimal


class ContractPriceIn(BaseModel):
    material_id: str
    price: Decimal
    currency: str = "DKK"
    valid: bool
    contract_ref: str = ""


class Difference(BaseModel):
    kind: Kind
    line_no: int | None = None
    material_id: str | None = None
    detail: str
    against: str | None = None
    expected: Decimal | None = None
    actual: Decimal | None = None
    delta: Decimal | None = None
    delta_pct: Decimal | None = None
    total_delta: Decimal | None = None
    reference_line_value: Decimal | None = None
    contract_ref: str | None = None
    within_tolerance: bool | None = None


class MatchResult(BaseModel):
    invoice_id: str
    po_id: str | None
    supplier_id: str
    invoice_amount: Decimal
    currency: str = "DKK"
    three_way: bool
    differences: list[Difference]
    tolerance_applied: dict[str, str] = {}

    @property
    def matched(self) -> bool:
        return not self.differences

    @property
    def within_tolerance(self) -> bool:
        return all(d.within_tolerance for d in self.differences)

    @property
    def blocking(self) -> list[Difference]:
        return [d for d in self.differences if not d.within_tolerance]

    def total_deviation(self) -> Decimal:
        return money(sum((abs(d.total_delta or Decimal(0)) for d in self.differences), Decimal(0)))

    def summary(self) -> str:
        if not self.differences:
            return f"Invoice {self.invoice_id} matches PO {self.po_id}, the goods receipts and the contract prices."
        parts = [d.detail for d in self.differences]
        return f"Invoice {self.invoice_id}: " + " ".join(parts)


def _pct(delta: Decimal, expected: Decimal) -> Decimal | None:
    if expected == 0:
        return None
    return (delta / expected * 100).quantize(CENT)


def match(
    invoice: InvoiceIn,
    po: PoIn | None,
    receipts: list[ReceiptIn],
    contract_prices: dict[str, ContractPriceIn],
    supplier_blocked: bool = False,
) -> MatchResult:
    """Compare an invoice with its PO (two-way), the goods receipts (three-way) and the contract prices.
    Reports EVERY difference; applies no tolerance (that is `apply_policy`)."""
    diffs: list[Difference] = []
    if supplier_blocked:
        diffs.append(Difference(kind="supplier_blocked", detail=f"Supplier {invoice.supplier_id} is blocked in SAP."))
    if po is None:
        diffs.append(
            Difference(kind="no_po", detail=f"Invoice {invoice.id} names no PO (or the PO could not be read).")
        )
    elif po.supplier_id != invoice.supplier_id:
        diffs.append(
            Difference(
                kind="supplier_mismatch",
                detail=f"The invoice is from {invoice.supplier_id} but PO {po.id} belongs to {po.supplier_id}.",
            )
        )
    po_lines = {ln.material_id: ln for ln in po.lines} if po else {}
    received: dict[int, Decimal] = {}
    for r in receipts:
        received[r.line_no] = received.get(r.line_no, Decimal(0)) + r.quantity
    for line in invoice.lines:
        contract = contract_prices.get(line.material_id)
        if contract is not None and contract.valid and contract.currency != invoice.currency:
            diffs.append(
                Difference(
                    kind="currency",
                    line_no=line.line_no,
                    material_id=line.material_id,
                    detail=f"Invoice currency {invoice.currency} differs from the contract price currency {contract.currency}.",
                )
            )
        po_line = po_lines.get(line.material_id)
        if po is not None and po_line is None:
            diffs.append(
                Difference(
                    kind="line_not_on_po",
                    line_no=line.line_no,
                    material_id=line.material_id,
                    detail=f"{line.material_id} is invoiced but is not on PO {po.id}.",
                )
            )
        reference, against, ref = None, None, ""
        if contract is not None and contract.valid:
            reference, against, ref = contract.price, "contract", contract.contract_ref
        elif po_line is not None:
            reference, against = po_line.price, "po"
        if reference is not None and line.price != reference:
            delta = line.price - reference
            diffs.append(
                Difference(
                    kind="price",
                    line_no=line.line_no,
                    material_id=line.material_id,
                    against=against,
                    contract_ref=ref or None,
                    expected=reference,
                    actual=line.price,
                    delta=delta,
                    delta_pct=_pct(delta, reference),
                    total_delta=money(delta * line.quantity),
                    reference_line_value=money(reference * line.quantity),
                    detail=f"{line.material_id}: invoiced {line.price} per unit, {against} price {reference}"
                    + (f" ({ref})" if ref else "")
                    + f", difference {money(delta * line.quantity)} on {line.quantity}.",
                )
            )
        if po_line is not None:
            if line.quantity > po_line.quantity:
                delta = line.quantity - po_line.quantity
                diffs.append(
                    Difference(
                        kind="quantity_over_po",
                        line_no=line.line_no,
                        material_id=line.material_id,
                        against="po",
                        expected=po_line.quantity,
                        actual=line.quantity,
                        delta=delta,
                        delta_pct=_pct(delta, po_line.quantity),
                        total_delta=money(delta * line.price),
                        detail=f"{line.material_id}: invoiced {line.quantity}, ordered {po_line.quantity}.",
                    )
                )
            got = received.get(po_line.line_no)
            if got is None:
                diffs.append(
                    Difference(
                        kind="not_received",
                        line_no=line.line_no,
                        material_id=line.material_id,
                        against="goods_receipt",
                        expected=Decimal(0),
                        actual=line.quantity,
                        detail=f"{line.material_id} on PO {po.id if po else ''} line {po_line.line_no} has no goods receipt.",
                    )
                )
            elif line.quantity > got:
                delta = line.quantity - got
                diffs.append(
                    Difference(
                        kind="quantity",
                        line_no=line.line_no,
                        material_id=line.material_id,
                        against="goods_receipt",
                        expected=got,
                        actual=line.quantity,
                        delta=delta,
                        delta_pct=_pct(delta, got),
                        total_delta=money(delta * line.price),
                        detail=f"{line.material_id}: invoiced {line.quantity}, received {got}.",
                    )
                )
    lines_total = sum((ln.quantity * ln.price for ln in invoice.lines), Decimal(0))
    if money(lines_total) != money(invoice.amount):
        delta = invoice.amount - lines_total
        diffs.append(
            Difference(
                kind="amount",
                expected=money(lines_total),
                actual=invoice.amount,
                delta=money(delta),
                delta_pct=_pct(delta, lines_total),
                detail=f"The invoice total {invoice.amount} differs from its lines, {money(lines_total)}.",
            )
        )
    return MatchResult(
        invoice_id=invoice.id,
        po_id=po.id if po else None,
        supplier_id=invoice.supplier_id,
        invoice_amount=invoice.amount,
        currency=invoice.currency,
        three_way=po is not None and bool(receipts),
        differences=diffs,
    )


def apply_policy(
    result: MatchResult, pct: Decimal, abs_amount: Decimal, quantity_tolerance: Decimal = Decimal(0)
) -> MatchResult:
    """The policy's tolerance: a price line may differ by up to `pct`% of the contract line value or `abs_amount`,
    whichever is higher; quantity tolerance is `quantity_tolerance` (0 = none). Everything else is never tolerated."""
    for d in result.differences:
        if d.kind == "price" and d.total_delta is not None and d.reference_line_value is not None:
            allowed = max(d.reference_line_value * pct / 100, abs_amount)
            d.within_tolerance = abs(d.total_delta) <= allowed
        elif d.kind in ("quantity", "quantity_over_po") and d.delta is not None:
            d.within_tolerance = abs(d.delta) <= quantity_tolerance
        else:
            d.within_tolerance = False
    result.tolerance_applied = {"pct": str(pct), "abs": str(abs_amount), "quantity_tolerance": str(quantity_tolerance)}
    return result


def rematch_with_credit_note(
    result: MatchResult, credit_note_ref: str | None, credit_amount: Decimal | None
) -> dict[str, Any]:
    """Only the price difference covered by a confirmed credit note may remain open."""
    if not credit_note_ref:
        return {"ok": False, "reason": "no confirmed credit note", "open_difference": str(result.total_deviation())}
    price_open = money(
        sum((abs(d.total_delta or Decimal(0)) for d in result.differences if d.kind == "price"), Decimal(0))
    )
    others = [d.detail for d in result.differences if d.kind != "price"]
    covered = credit_amount is None or money(credit_amount) >= price_open
    return {
        "ok": covered and not others,
        "credit_note_ref": credit_note_ref,
        "price_difference": str(price_open),
        "credit_amount": str(credit_amount) if credit_amount is not None else "unstated",
        "other_open_differences": others,
        "reason": ""
        if covered and not others
        else ("other deviations remain" if others else "credit note does not cover the difference"),
    }
