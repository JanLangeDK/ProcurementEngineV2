"""Simulated document repository."""

from __future__ import annotations

from typing import Any

from pev2 import config, store
from pev2.errors import BusinessException
from pev2.tools.base import ToolSpec, registry


def get_document(args: dict[str, Any]) -> dict[str, Any]:
    row = store.one("select * from dms_document where id = ?", (args["document_id"],))
    if row is None:
        raise BusinessException("not_found", f"Document {args['document_id']} does not exist.")
    return {"content_ref": row["content_ref"], "mime_type": row["mime_type"], "metadata": store.loads(row["metadata"])}


def store_document(args: dict[str, Any]) -> dict[str, Any]:
    doc_id = args.get("document_id") or f"DOC-{int(store.first('select count(*) as n from dms_document')['n']) + 1:05d}"
    store.run(
        "insert or replace into dms_document values (?, ?, ?, ?, ?)",
        (
            doc_id,
            args.get("content_ref", ""),
            args.get("mime_type", "application/pdf"),
            store.dumps(args.get("metadata", {})),
            config.now().isoformat(),
        ),
    )
    return {"document_id": doc_id}


def register() -> None:
    registry.register(
        ToolSpec(
            id="dms.get_document",
            name="Retrieve document",
            integration="dms",
            side_effect="read",
            permissions=("dms.read",),
        ),
        get_document,
    )
    registry.register(
        ToolSpec(
            id="dms.store_document",
            name="Store document",
            integration="dms",
            side_effect="write",
            idempotent=True,
            permissions=("dms.write",),
        ),
        store_document,
    )
