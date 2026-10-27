"""Simulated enterprise knowledge base: the markdown documents of fixtures/brain served as tools.

`fetch_playbook` returns a document and the structured facts the services need (tolerances, terms), parsed
from the policy text so that changing the document changes the behaviour.
"""

from __future__ import annotations

import re
from decimal import Decimal
from pathlib import Path
from typing import Any

from pev2 import config, store
from pev2.errors import BusinessException
from pev2.tools.base import ToolSpec, registry

DOCS = config.FIXTURES / "brain"

PLAYBOOKS = {
    "invoice-handling": "policies/invoice-handling.md",
    "ap-specialist": "playbooks/ap-specialist.md",
    "supplier-manager": "playbooks/supplier-manager.md",
    "buyer": "playbooks/buyer.md",
    "concierge": "playbooks/concierge.md",
    "payment-question": "processes/supplier-inquiries.md",
    "supplier-communication": "policies/supplier-communication.md",
    "authority-matrix": "policies/authority-matrix.md",
    "late-delivery": "processes/late-delivery.md",
    "procurement-policy": "policies/procurement-policy.md",
}


def _read(rel: str) -> str:
    path = DOCS / rel
    if not path.exists():
        raise BusinessException("not_found", f"Document {rel} is not in the knowledge base.")
    return path.read_text(encoding="utf-8")


def parse_tolerance(text: str) -> dict[str, Any]:
    """'up to 1% of the contract line value or 200 DKK' → {'pct': 1, 'abs': 200, 'currency': 'DKK', 'quantity_tolerance': 0}."""
    pct = re.search(r"up to\s+(\d+(?:\.\d+)?)\s*%", text)
    amount = re.search(r"or\s+(\d+(?:[.,]\d+)?)\s*(DKK|EUR)", text)
    qty = re.search(r"Quantity:\s*tolerance\s*(\d+)", text)
    terms = re.search(r"payment terms are\s+(\d+)\s+days", text)
    weekday = re.search(r"Payment run weekday:\s*(\w+)", text)
    return {
        "pct": Decimal(pct.group(1)) if pct else Decimal("1"),
        "abs": Decimal(amount.group(1).replace(",", "")) if amount else Decimal("200"),
        "currency": amount.group(2) if amount else "DKK",
        "quantity_tolerance": Decimal(qty.group(1)) if qty else Decimal("0"),
        "payment_terms_days": int(terms.group(1)) if terms else 30,
        "payment_run_weekday": weekday.group(1).lower() if weekday else config.payment_run_weekday(),
    }


def fetch_playbook(args: dict[str, Any]) -> dict[str, Any]:
    key = args.get("playbook", "invoice-handling")
    rel = PLAYBOOKS.get(key, key)
    text = _read(rel)
    facts = parse_tolerance(text) if "invoice" in key or "payment" in key else {}
    return {
        "playbook": key,
        "content": text,
        "version": str(int((DOCS / rel).stat().st_mtime)),
        "facts": {k: str(v) for k, v in facts.items()},
    }


def supplier_brief(args: dict[str, Any]) -> dict[str, Any]:
    sid = args["supplier_id"]
    path = DOCS / "suppliers" / f"{sid}.md"
    profile = store.one("select * from sap_supplier_profile where supplier_id = ?", (sid,))
    supplier = store.one("select * from sap_supplier where id = ?", (sid,))
    if supplier is None:
        raise BusinessException("not_found", f"Supplier {sid} is unknown.")
    contracts = store.rows("select * from sap_contract where supplier_id = ? order by valid_to desc", (sid,))
    history = []
    if profile:
        history.append(f"Earlier late deliveries: {profile['late_deliveries']}")
        history.append(f"Typical lead time: {profile['lead_time_days']} days")
        if profile["restrictions"]:
            history.append(f"Restrictions: {profile['restrictions']}")
    notes = [
        m["summary"]
        for m in store.rows(
            "select summary from kb_memory where entities like ? order by id desc limit 5", (f"%{sid}%",)
        )
    ]
    return {
        "profile": {
            **dict(supplier),
            "blocked": bool(supplier["blocked"]),
            "lead_time_days": profile["lead_time_days"] if profile else None,
            "restrictions": profile["restrictions"] if profile else "",
        },
        "contacts": [supplier["email"]],
        "history": history + notes,
        "contract_facts": [
            f"{c['id']} {c['status']} {c['valid_from']}–{c['valid_to']}"
            + (" (penalty clause for late delivery)" if "chemi" in str(c["title"]).lower() else "")
            for c in contracts
        ],
        "document": path.read_text(encoding="utf-8") if path.exists() else "",
    }


def ask(args: dict[str, Any]) -> dict[str, Any]:
    """Keyword retrieval over the documents (the real brain is a graph + vector index)."""
    words = [w for w in re.findall(r"[a-z0-9]+", args["question"].lower()) if len(w) > 3]
    hits: list[tuple[int, Path, str]] = []
    for path in sorted(DOCS.rglob("*.md")):
        text = path.read_text(encoding="utf-8")
        low = text.lower()
        score = sum(low.count(w) for w in words)
        if score:
            hits.append((score, path, text))
    hits.sort(key=lambda h: -h[0])
    if not hits:
        raise BusinessException("no_answer", "Nothing in the knowledge base matches the question.")
    best = hits[0]
    body = re.sub(r"\A---.*?---\s*", "", best[2], count=1, flags=re.S)  # the frontmatter is metadata, not an answer
    paragraphs = [p.strip() for p in body.split("\n\n") if any(w in p.lower() for w in words)]
    return {
        "answer": (paragraphs[0] if paragraphs else body[:600])[:800],
        "citations": [str(h[1].relative_to(DOCS)) for h in hits[:3]],
    }


def remember_case(args: dict[str, Any]) -> dict[str, Any]:
    store.run(
        "insert into kb_memory (case_id, summary, entities, created_at) values (?, ?, ?, ?)",
        (args.get("case_id"), args.get("summary", ""), ",".join(args.get("entities", [])), config.now().isoformat()),
    )
    return {"stored": True}


def register() -> None:
    for tid, name, fn, se, perms in [
        ("brain.fetch_playbook", "Fetch playbook", fetch_playbook, "read", ("brain.read",)),
        ("brain.supplier_brief", "Supplier brief", supplier_brief, "read", ("brain.read",)),
        ("brain.ask", "Ask the knowledge base", ask, "read", ("brain.read",)),
        ("brain.remember_case", "Remember case outcome", remember_case, "write", ("brain.write",)),
    ]:
        registry.register(
            ToolSpec(id=tid, name=name, integration="brain", side_effect=se, idempotent=True, permissions=perms), fn
        )
