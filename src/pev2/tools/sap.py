"""Simulated SAP: the tables of fixtures/sap loaded into SQLite. Reads and the few controlled writes of section 3."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from pev2 import config, store
from pev2.errors import BusinessException, LimitError
from pev2.tools.base import ToolSpec, registry


def _d(v: Any) -> str:
    return str(Decimal(str(v)))


def get_invoice(args: dict[str, Any]) -> dict[str, Any]:
    if args.get("invoice_id") or args.get("document_number"):
        row = store.one(
            "select * from sap_invoice where id = ?", (args.get("invoice_id") or args.get("document_number"),)
        )
        if row is None:
            raise BusinessException(
                "not_found", f"Invoice {args.get('invoice_id') or args.get('document_number')} does not exist."
            )
        return _invoice(row)
    sql, params = "select * from sap_invoice where 1=1", []
    if args.get("supplier_id"):
        sql += " and supplier_id = ?"
        params.append(args["supplier_id"])
    if args.get("invoice_number"):
        sql += " and invoice_number = ?"
        params.append(args["invoice_number"])
    return {"invoices": [_invoice(r) for r in store.q(sql, tuple(params))]}


def _invoice(row: Any) -> dict[str, Any]:
    lines = store.rows(
        "select line_no, material_id, quantity, price from sap_invoice_line where invoice_id = ? order by line_no",
        (row["id"],),
    )
    return {**dict(row), "lines": lines}


def find_invoice_by_supplier_reference(args: dict[str, Any]) -> dict[str, Any]:
    rows = store.q(
        "select * from sap_invoice where supplier_id = ? and invoice_number = ?",
        (args["supplier_id"], args["supplier_invoice_number"]),
    )
    if not rows:
        raise BusinessException(
            "not_found",
            f"No invoice from {args['supplier_id']} with the supplier's number {args['supplier_invoice_number']}.",
        )
    if len(rows) > 1:
        raise BusinessException(
            "ambiguous_match", f"{len(rows)} invoices carry the supplier's number {args['supplier_invoice_number']}."
        )
    r = rows[0]
    return {
        "document_number": r["id"],
        "amount": r["amount"],
        "currency": r["currency"],
        "posting_date": (r["received_at"] or "")[:10],
        "status": r["status"],
        "due_date": r["due_date"],
    }


def get_purchase_order(args: dict[str, Any]) -> dict[str, Any]:
    row = store.one("select * from sap_purchase_order where id = ?", (args["po_number"],))
    if row is None:
        raise BusinessException("not_found", f"Purchase order {args['po_number']} does not exist.")
    lines = store.rows(
        "select line_no, material_id, quantity, price from sap_po_line where po_id = ? order by line_no", (row["id"],)
    )
    return {**dict(row), "po_number": row["id"], "items": lines, "currency": "DKK"}


def find_purchase_orders(args: dict[str, Any]) -> dict[str, Any]:
    sql, params = (
        "select distinct p.* from sap_purchase_order p left join sap_po_line l on l.po_id = p.id where 1=1",
        [],
    )
    if args.get("supplier_id"):
        sql += " and p.supplier_id = ?"
        params.append(args["supplier_id"])
    if args.get("material_id"):
        sql += " and l.material_id = ?"
        params.append(args["material_id"])
    if args.get("status"):
        sql += " and p.status = ?"
        params.append(args["status"])
    return {"purchase_orders": [dict(r) for r in store.q(sql, tuple(params))]}


def get_goods_receipt(args: dict[str, Any]) -> dict[str, Any]:
    return {
        "receipts": store.rows("select * from sap_goods_receipt where po_id = ? order by line_no", (args["po_number"],))
    }


def get_contract(args: dict[str, Any]) -> dict[str, Any]:
    sql, params = (
        "select cp.*, c.status as contract_status from sap_contract_price cp left join sap_contract c on c.id = cp.contract_ref where 1=1",
        [],
    )
    for key in ("supplier_id", "material_id"):
        if args.get(key):
            sql += f" and cp.{key} = ?"
            params.append(args[key])
    if args.get("contract_id"):
        sql += " and cp.contract_ref = ?"
        params.append(args["contract_id"])
    prices = store.rows(sql, tuple(params))
    today = config.today().isoformat()
    for p in prices:
        p["valid"] = p["valid_from"] <= today <= p["valid_to"]
    if not prices:
        raise BusinessException("not_found", "No purchasing contract matches.")
    return {
        "contract_id": prices[0]["contract_ref"],
        "prices": prices,
        "valid_from": prices[0]["valid_from"],
        "valid_to": prices[0]["valid_to"],
        "conditions": ["prices per contract line", "payment terms per supplier master"],
    }


def get_supplier(args: dict[str, Any]) -> dict[str, Any]:
    if args.get("supplier_id"):
        rows = store.q("select * from sap_supplier where id = ?", (args["supplier_id"],))
    elif args.get("email"):
        rows = store.q("select * from sap_supplier where lower(email) = lower(?)", (args["email"],))
    elif args.get("name"):
        rows = store.q("select * from sap_supplier where lower(name) like lower(?)", (f"%{args['name']}%",))
    else:
        rows = []
    if not rows:
        raise BusinessException("not_found", "No supplier matches.")
    if len(rows) > 1:
        raise BusinessException("ambiguous_match", f"{len(rows)} suppliers match.")
    r = dict(rows[0])
    r["blocked"] = bool(r["blocked"])
    r["approved"] = True
    return r


def get_material(args: dict[str, Any]) -> dict[str, Any]:
    row = store.one("select * from sap_material where id = ?", (args["material_id"],))
    if row is None:
        raise BusinessException("not_found", f"Material {args['material_id']} does not exist in the material master.")
    return {**dict(row), "specifications": {"revision": "current", "category": row["category"]}, "unit": row["uom"]}


def get_stock(args: dict[str, Any]) -> dict[str, Any]:
    sql, params = "select * from sap_stock where material_id = ?", [args["material_id"]]
    if args.get("plant"):
        sql += " and plant = ?"
        params.append(args["plant"])
    rows = store.rows(sql, tuple(params))
    available = sum(Decimal(r["on_hand"]) for r in rows)
    return {"available": str(available), "reserved": "0", "unit": "pcs", "by_plant": rows}


def get_payment_terms(args: dict[str, Any]) -> dict[str, Any]:
    inv = store.one(
        "select i.*, s.payment_terms_days from sap_invoice i join sap_supplier s on s.id = i.supplier_id where i.id = ?",
        (args["document_number"],),
    )
    if inv is None:
        raise BusinessException("not_found", f"Invoice {args['document_number']} does not exist.")
    return {
        "terms_days": int(inv["payment_terms_days"]),
        "baseline_date": (inv["received_at"] or "")[:10],
        "payment_run_days": [config.payment_run_weekday()],
    }


def get_supplier_confirmation(args: dict[str, Any]) -> dict[str, Any]:
    row = store.one("select * from sap_confirmation where po_id = ?", (args["po_number"],))
    if row is None:
        return {"confirmations": [], "status": "pending"}
    return {
        "confirmations": [dict(row)],
        "status": row["status"],
        "confirmed_date": row["confirmed_date"],
        "note": row["note"],
    }


# ---- writes (hard limits in limits.py, idempotency in base.py) ------------------------------------
def block_invoice(args: dict[str, Any]) -> dict[str, Any]:
    row = store.one("select * from sap_invoice where id = ?", (args["document_number"],))
    if row is None:
        raise BusinessException("not_found", f"Invoice {args['document_number']} does not exist.")
    if row["status"] in ("paid", "posted"):
        raise LimitError(f"Invoice {row['id']} is {row['status']} and cannot be blocked.")
    store.run(
        "update sap_invoice set status = 'blocked', block_reason = ? where id = ?", (args.get("reason", ""), row["id"])
    )
    return {"document_number": row["id"], "blocked": True}


def park_invoice(args: dict[str, Any]) -> dict[str, Any]:
    inv = args.get("invoice") or {}
    doc = inv.get("id") or args.get("document_number")
    row = store.one("select * from sap_invoice where id = ?", (doc,))
    if row is None:
        raise BusinessException("not_found", f"Invoice {doc} does not exist.")
    store.run(
        "update sap_invoice set status = 'parked', note = ?, credit_note_ref = coalesce(?, credit_note_ref) where id = ?",
        (args.get("notes", ""), args.get("credit_note_ref"), row["id"]),
    )
    return {"document_number": row["id"], "status": "parked"}


def release_invoice(args: dict[str, Any]) -> dict[str, Any]:
    row = store.one("select * from sap_invoice where id = ?", (args["document_number"],))
    if row is None:
        raise BusinessException("not_found", f"Invoice {args['document_number']} does not exist.")
    if row["status"] != "parked":
        raise LimitError(f"Only a parked invoice can be released; {row['id']} is {row['status']}.")
    store.run(
        "update sap_invoice set status = 'posted', released_by = ? where id = ?",
        (args.get("released_by", "AP Team Lead"), row["id"]),
    )
    return {"document_number": row["id"], "released": True}


def submit_invoice(args: dict[str, Any]) -> dict[str, Any]:
    inv = args.get("invoice") or {}
    doc = inv.get("id") or args.get("document_number")
    row = store.one("select * from sap_invoice where id = ?", (doc,))
    if row is None:
        raise BusinessException("not_found", f"Invoice {doc} does not exist.")
    store.run(
        "update sap_invoice set status = 'posted', note = coalesce(note, '') || ' posted to AP workflow' where id = ?",
        (row["id"],),
    )
    return {"document_number": row["id"], "status": "posted"}


def create_purchase_order(args: dict[str, Any]) -> dict[str, Any]:
    n = store.first("select count(*) as n from sap_purchase_order")
    po_id = f"PO{45200 + int(n['n'])}"
    store.run(
        "insert into sap_purchase_order (id, supplier_id, status, requester, plant, delivery_date, created_by, created_at, approval_ref) values (?, ?, 'released', ?, ?, ?, 'workflow', ?, ?)",
        (
            po_id,
            args["supplier_id"],
            args.get("requester", ""),
            args.get("plant", ""),
            args.get("required_date", ""),
            config.now().isoformat(),
            args.get("approval_ref"),
        ),
    )
    for i, item in enumerate(args.get("items", []), start=1):
        store.run(
            "insert into sap_po_line values (?, ?, ?, ?, ?)",
            (po_id, i, item["material_id"], _d(item["quantity"]), _d(item["price"])),
        )
    return {"po_number": po_id, "status": "released"}


def update_po_delivery_date(args: dict[str, Any]) -> dict[str, Any]:
    row = store.one("select * from sap_purchase_order where id = ?", (args["po_number"],))
    if row is None:
        raise BusinessException("not_found", f"Purchase order {args['po_number']} does not exist.")
    store.run(
        "update sap_purchase_order set confirmed_date = ? where id = ?", (args["new_date"], row["id"])
    )  # the original delivery_date is kept
    return {
        "po_number": row["id"],
        "updated": True,
        "confirmed_date": args["new_date"],
        "original_due": row["delivery_date"],
    }


# ---- simulator helpers (not tools: the UI's "events" use them) ----------------------------------------
def post_confirmation(po_id: str, status: str, confirmed_date: str | None, note: str = "") -> None:
    store.run("insert or replace into sap_confirmation values (?, ?, ?, ?)", (po_id, status, confirmed_date, note))


def register() -> None:
    P = "OAuth2"  # noqa: N806
    for tid, name, fn, se, idem, perms, desc in [
        (
            "sap.get_invoice",
            "Retrieve invoice information",
            get_invoice,
            "read",
            True,
            ("sap.invoice.read",),
            "Read posted/parked supplier invoices to detect duplicates and status.",
        ),
        (
            "sap.find_invoice_by_supplier_reference",
            "Find invoice by supplier reference",
            find_invoice_by_supplier_reference,
            "read",
            True,
            ("sap.invoice.read",),
            "Our invoice document by the supplier's own invoice number.",
        ),
        (
            "sap.get_purchase_order",
            "Retrieve purchase order",
            get_purchase_order,
            "read",
            True,
            ("sap.purchasing.read",),
            "",
        ),
        (
            "sap.find_purchase_orders",
            "Search purchase orders",
            find_purchase_orders,
            "read",
            True,
            ("sap.purchasing.read",),
            "",
        ),
        (
            "sap.get_goods_receipt",
            "Retrieve goods receipts",
            get_goods_receipt,
            "read",
            True,
            ("sap.inventory.read",),
            "",
        ),
        ("sap.get_contract", "Retrieve purchasing contract", get_contract, "read", True, ("sap.contract.read",), ""),
        ("sap.get_supplier", "Retrieve supplier", get_supplier, "read", True, ("sap.supplier.read",), ""),
        ("sap.get_material", "Retrieve material master data", get_material, "read", True, ("sap.material.read",), ""),
        ("sap.get_stock", "Retrieve inventory levels", get_stock, "read", True, ("sap.inventory.read",), ""),
        (
            "sap.get_payment_terms",
            "Retrieve payment terms and payment run calendar",
            get_payment_terms,
            "read",
            True,
            ("sap.invoice.read",),
            "",
        ),
        (
            "sap.get_supplier_confirmation",
            "Retrieve order confirmations",
            get_supplier_confirmation,
            "read",
            True,
            ("sap.purchasing.read",),
            "",
        ),
        (
            "sap.block_invoice",
            "Block invoice for payment",
            block_invoice,
            "write",
            True,
            ("sap.invoice.write",),
            "Financial write.",
        ),
        (
            "sap.park_invoice",
            "Park invoice for release",
            park_invoice,
            "write",
            False,
            ("sap.invoice.write",),
            "Financial write.",
        ),
        (
            "sap.release_invoice",
            "Release invoice",
            release_invoice,
            "write",
            True,
            ("sap.invoice.release",),
            "Financial write; needs the AP Team Lead's decided approval.",
        ),
        (
            "sap.submit_invoice",
            "Submit invoice to financial workflow",
            submit_invoice,
            "write",
            False,
            ("sap.invoice.write",),
            "Financial write.",
        ),
        (
            "sap.create_purchase_order",
            "Create purchase order",
            create_purchase_order,
            "write",
            False,
            ("sap.purchasing.write", "sap.purchasing.release"),
            "Financial write; above 25,000 EUR needs the Procurement Manager's approval.",
        ),
        (
            "sap.update_po_delivery_date",
            "Update PO delivery date",
            update_po_delivery_date,
            "write",
            True,
            ("sap.purchasing.write",),
            "",
        ),
    ]:
        registry.register(
            ToolSpec(
                id=tid,
                name=name,
                integration="sap",
                side_effect=se,
                idempotent=idem,
                permissions=perms,
                description=desc,
                max_attempts=1
                if tid in ("sap.park_invoice", "sap.release_invoice", "sap.submit_invoice", "sap.create_purchase_order")
                else 3,
            ),
            fn,
        )
    _ = P
