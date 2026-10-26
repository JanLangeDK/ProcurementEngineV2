"""The one SQLite file: the simulated systems (SAP, cases, mail, documents, knowledge memory), the durable
workflow state, approvals and the audit log. Plain SQL, no ORM. `reset()` reloads the fixtures.

Money is stored as text and handled as Decimal in code.
"""

from __future__ import annotations

import csv
import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from pev2 import config

SCHEMA = """
create table if not exists sap_supplier (id text primary key, name text, country text, email text, phone text, payment_terms_days integer, blocked integer);
create table if not exists sap_material (id text primary key, description text, category text, uom text);
create table if not exists sap_contract (id text primary key, supplier_id text, title text, category text, valid_from text, valid_to text, status text);
create table if not exists sap_contract_price (supplier_id text, material_id text, price text, currency text, valid_from text, valid_to text, contract_ref text, primary key (supplier_id, material_id));
create table if not exists sap_purchase_order (id text primary key, supplier_id text, status text, requester text, plant text, delivery_date text, created_by text, created_at text, released_by text, released_at text, confirmed_date text, approval_ref text);
create table if not exists sap_po_line (po_id text, line_no integer, material_id text, quantity text, price text, primary key (po_id, line_no));
create table if not exists sap_goods_receipt (id text primary key, po_id text, line_no integer, quantity text, received_at text);
create table if not exists sap_invoice (id text primary key, supplier_id text, po_id text, invoice_number text, amount text, currency text, status text, received_at text, due_date text, paid_at text, note text, created_by text, released_by text, block_reason text, credit_note_ref text);
create table if not exists sap_invoice_line (invoice_id text, line_no integer, material_id text, quantity text, price text, primary key (invoice_id, line_no));
create table if not exists sap_stock (material_id text, plant text, on_hand text, reorder_point text, reorder_qty text, primary key (material_id, plant));
create table if not exists sap_confirmation (po_id text primary key, status text, confirmed_date text, note text);
create table if not exists sap_approved_supplier (material_category text, supplier_id text, primary key (material_category, supplier_id));
create table if not exists sap_supplier_profile (supplier_id text primary key, lead_time_days integer, restrictions text, late_deliveries integer);

create table if not exists case_issue (key text primary key, project text, summary text, description text, issue_type text, status text, assignee text, created_at text, updated_at text, instance_id text);
create table if not exists case_comment (id integer primary key autoincrement, issue_key text, body text, created_at text);
create table if not exists mail_message (id integer primary key autoincrement, direction text, status text, thread_id text, to_addr text, from_addr text, subject text, body text, approval_ref text, created_at text, sent_at text, instance_id text);
create table if not exists dms_document (id text primary key, content_ref text, mime_type text, metadata text, created_at text);
create table if not exists kb_memory (id integer primary key autoincrement, case_id text, summary text, entities text, created_at text);

create table if not exists wf_instance (id text primary key, workflow text, status text, trigger text, state text, current_step text, end_state text, park_reason text, created_at text, updated_at text);
create table if not exists wf_event (id integer primary key autoincrement, kind text, correlation text, payload text, created_at text, consumed_by text);
create table if not exists wf_approval (id text primary key, instance_id text, name text, owner text, payload text, status text, decided_by text, decision_note text, requested_at text, due_at text, decided_at text, escalated_to text);
create table if not exists wf_idempotency (key text primary key, tool text, result text, created_at text);
create table if not exists wf_audit (id integer primary key autoincrement, instance_id text, step text, kind text, component text, status text, detail text, at text);
create table if not exists wf_review (id integer primary key autoincrement, instance_id text, step text, reason text, payload text, status text, created_at text, decided_at text);
"""

FIXTURE_TABLES = {
    "supplier.csv": ("sap_supplier", ["id", "name", "country", "email", "phone", "payment_terms_days", "blocked"]),
    "material.csv": ("sap_material", ["id", "description", "category", "uom"]),
    "contract.csv": ("sap_contract", ["id", "supplier_id", "title", "category", "valid_from", "valid_to", "status"]),
    "contract_price.csv": (
        "sap_contract_price",
        ["supplier_id", "material_id", "price", "currency", "valid_from", "valid_to", "contract_ref"],
    ),
    "purchase_order.csv": (
        "sap_purchase_order",
        [
            "id",
            "supplier_id",
            "status",
            "requester",
            "plant",
            "delivery_date",
            "created_by",
            "created_at",
            "released_by",
            "released_at",
        ],
    ),
    "po_line.csv": ("sap_po_line", ["po_id", "line_no", "material_id", "quantity", "price"]),
    "goods_receipt.csv": ("sap_goods_receipt", ["id", "po_id", "line_no", "quantity", "received_at"]),
    "invoice.csv": (
        "sap_invoice",
        [
            "id",
            "supplier_id",
            "po_id",
            "invoice_number",
            "amount",
            "currency",
            "status",
            "received_at",
            "due_date",
            "paid_at",
            "note",
            "created_by",
            "released_by",
        ],
    ),
    "invoice_line.csv": ("sap_invoice_line", ["invoice_id", "line_no", "material_id", "quantity", "price"]),
    "stock.csv": ("sap_stock", ["material_id", "plant", "on_hand", "reorder_point", "reorder_qty"]),
}


def _connect(path: Path | None = None) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path or config.db_path()), timeout=30, isolation_level=None, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("pragma journal_mode=wal")
    conn.execute("pragma foreign_keys=on")
    return conn


_shared: sqlite3.Connection | None = None
_shared_path: Path | None = None


def connection() -> sqlite3.Connection:
    """One process-wide connection (SQLite + WAL is fine for the demo; a real deployment uses Postgres)."""
    global _shared, _shared_path
    path = config.db_path()
    if _shared is None or _shared_path != path:
        _shared = _connect(path)
        _shared_path = path
        _shared.executescript(SCHEMA)
    return _shared


@contextmanager
def tx() -> Iterator[sqlite3.Connection]:
    conn = connection()
    conn.execute("begin")
    try:
        yield conn
        conn.execute("commit")
    except Exception:
        conn.execute("rollback")
        raise


def init() -> None:
    connection()


def q(sql: str, params: tuple[Any, ...] = ()) -> list[sqlite3.Row]:
    return connection().execute(sql, params).fetchall()


def one(sql: str, params: tuple[Any, ...] = ()) -> sqlite3.Row | None:
    row: sqlite3.Row | None = connection().execute(sql, params).fetchone()
    return row


def first(sql: str, params: tuple[Any, ...] = ()) -> sqlite3.Row:
    """Like one() for queries that always have a row (counts, last_insert_rowid)."""
    row = one(sql, params)
    if row is None:
        raise RuntimeError(f"no row for {sql}")
    return row


def run(sql: str, params: tuple[Any, ...] = ()) -> None:
    connection().execute(sql, params)


def rows(sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
    return [dict(r) for r in q(sql, params)]


def load_fixtures() -> None:
    """Load the SAP CSV fixtures (idempotent: tables are emptied first) and derive the approved-supplier
    list and supplier profiles that the purchase-request stories need."""
    conn = connection()
    for file, (table, cols) in FIXTURE_TABLES.items():
        conn.execute(f"delete from {table}")
        with open(config.FIXTURES / "sap" / file, newline="", encoding="utf-8") as fh:
            reader = csv.DictReader(fh)
            for row in reader:
                values = [row.get(c, "") for c in cols]
                if table == "sap_supplier":
                    values[-1] = 1 if str(row.get("blocked", "")).lower() == "true" else 0
                conn.execute(f"insert into {table} ({', '.join(cols)}) values ({', '.join('?' * len(cols))})", values)
    # approved suppliers per material category: every supplier with an active contract price in that category
    conn.execute("delete from sap_approved_supplier")
    conn.execute(
        """insert or ignore into sap_approved_supplier (material_category, supplier_id)
           select distinct m.category, cp.supplier_id from sap_contract_price cp join sap_material m on m.id = cp.material_id"""
    )
    conn.execute("delete from sap_supplier_profile")
    for r in conn.execute("select id from sap_supplier").fetchall():
        sid = r["id"]
        digits = int(sid[1:]) if sid[1:].isdigit() else 0
        lead = 3 + digits % 7
        late = conn.execute(
            "select count(*) from sap_purchase_order where supplier_id = ? and status = 'closed' and released_at > delivery_date",
            (sid,),
        ).fetchone()[0]
        restrictions = "single-source for packaging film" if sid == "S1001" else ""
        conn.execute("insert into sap_supplier_profile values (?, ?, ?, ?)", (sid, lead, restrictions, late))
    # story D's late delivery: PO45017 due before the clock, no goods receipt
    conn.execute("update sap_purchase_order set delivery_date = '2026-10-19', status = 'released' where id = 'PO45017'")
    conn.execute("delete from sap_goods_receipt where po_id = 'PO45017'")
    conn.execute("delete from sap_confirmation")


def reset(keep_fixtures: bool = False) -> None:
    """Empty the runtime tables; reload the simulated systems unless keep_fixtures."""
    conn = connection()
    for table in (
        "case_issue",
        "case_comment",
        "mail_message",
        "dms_document",
        "kb_memory",
        "wf_instance",
        "wf_event",
        "wf_approval",
        "wf_idempotency",
        "wf_audit",
        "wf_review",
    ):
        conn.execute(f"delete from {table}")
    if not keep_fixtures:
        load_fixtures()


def dumps(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, default=str)


def loads(text: str | None) -> Any:
    return json.loads(text) if text else None
