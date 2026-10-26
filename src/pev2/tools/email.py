"""Simulated mailbox: inbound messages (the UI posts them), drafts, approved sending, correspondence tracking."""

from __future__ import annotations

from typing import Any

from pev2 import config, store
from pev2.errors import BusinessException
from pev2.tools.base import ToolSpec, registry


def receive_event(args: dict[str, Any]) -> dict[str, Any]:
    rows = store.rows("select * from mail_message where direction = 'in' order by id desc limit 20")
    return {"messages": rows}


def get_attachments(args: dict[str, Any]) -> dict[str, Any]:
    row = store.one("select * from mail_message where id = ?", (args["message_id"],))
    if row is None:
        raise BusinessException("not_found", "Message does not exist.")
    return {"attachments": [{"name": "attachment.pdf", "content": row["body"], "untrusted": True}]}


def create_draft(args: dict[str, Any]) -> dict[str, Any]:
    to = args.get("to") or []
    store.run(
        "insert into mail_message (direction, status, thread_id, to_addr, from_addr, subject, body, created_at, instance_id) values ('out', 'draft', ?, ?, 'procurement@danishcrown.example', ?, ?, ?, ?)",
        (
            args.get("thread_id"),
            ", ".join(to) if isinstance(to, list) else str(to),
            args.get("subject", ""),
            args.get("body", ""),
            config.now().isoformat(),
            args.get("instance_id"),
        ),
    )
    did = store.first("select last_insert_rowid() as id")
    return {"draft_id": str(did["id"])}


def send_approved(args: dict[str, Any]) -> dict[str, Any]:
    row = store.one("select * from mail_message where id = ? and direction = 'out'", (args["draft_id"],))
    if row is None:
        raise BusinessException("not_found", f"Draft {args['draft_id']} does not exist.")
    if row["status"] == "sent":
        return {"message_id": str(row["id"]), "sent_at": row["sent_at"]}
    now = config.now().isoformat()
    store.run(
        "update mail_message set status = 'sent', sent_at = ?, approval_ref = ? where id = ?",
        (now, args.get("approval_reference") or args.get("policy"), row["id"]),
    )
    return {"message_id": str(row["id"]), "sent_at": now}


def track_correspondence(args: dict[str, Any]) -> dict[str, Any]:
    replies = store.rows(
        "select * from mail_message where direction = 'in' and thread_id = ? order by id", (args["thread_id"],)
    )
    deadline = args.get("deadline")
    timed_out = bool(deadline) and not replies and config.now().isoformat() > str(deadline)
    return {"replies": replies, "timed_out": timed_out}


# ---- simulator helpers ---------------------------------------------------------------------------------
def inbound(from_addr: str, subject: str, body: str, thread_id: str | None = None) -> int:
    store.run(
        "insert into mail_message (direction, status, thread_id, to_addr, from_addr, subject, body, created_at) values ('in', 'received', ?, 'procurement@danishcrown.example', ?, ?, ?, ?)",
        (thread_id, from_addr, subject, body, config.now().isoformat()),
    )
    return int(store.first("select last_insert_rowid() as id")["id"])


def register() -> None:
    for tid, name, fn, se, idem, perms, attempts in [
        ("email.receive_event", "Receive email event", receive_event, "read", True, ("mail.read",), 3),
        ("email.get_attachments", "Retrieve attachments", get_attachments, "read", True, ("mail.read",), 3),
        ("email.create_draft", "Create draft", create_draft, "write", False, ("mail.write",), 3),
        ("email.send_approved", "Send approved notification", send_approved, "notify", False, ("mail.send",), 1),
        ("email.track_correspondence", "Track correspondence", track_correspondence, "read", True, ("mail.read",), 3),
    ]:
        registry.register(
            ToolSpec(
                id=tid,
                name=name,
                integration="email",
                side_effect=se,
                idempotent=idem,
                permissions=perms,
                max_attempts=attempts,
            ),
            fn,
        )
