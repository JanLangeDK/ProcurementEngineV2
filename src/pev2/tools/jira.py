"""Simulated case management (Jira): issues, assignments, comments."""

from __future__ import annotations

from typing import Any

from pev2 import config, store
from pev2.errors import BusinessException
from pev2.tools.base import ToolSpec, registry


def create_issue(args: dict[str, Any]) -> dict[str, Any]:
    n = store.first("select count(*) as n from case_issue")
    key = f"{args.get('project', 'PROC')}-{1000 + int(n['n'])}"
    now = config.now().isoformat()
    store.run(
        "insert into case_issue (key, project, summary, description, issue_type, status, assignee, created_at, updated_at, instance_id) values (?, ?, ?, ?, ?, 'open', ?, ?, ?, ?)",
        (
            key,
            args.get("project", "PROC"),
            args.get("summary", ""),
            args.get("description", ""),
            args.get("issue_type", "case"),
            args.get("assignee"),
            now,
            now,
            args.get("instance_id"),
        ),
    )
    return {"issue_key": key}


def _issue(key: str) -> Any:
    row = store.one("select * from case_issue where key = ?", (key,))
    if row is None:
        raise BusinessException("not_found", f"Case {key} does not exist.")
    return row


def update_issue(args: dict[str, Any]) -> dict[str, Any]:
    row = _issue(args["issue_key"])
    fields = args.get("fields") or {}
    status = args.get("transition") or fields.get("status") or row["status"]
    store.run(
        "update case_issue set status = ?, summary = coalesce(?, summary), updated_at = ? where key = ?",
        (status, fields.get("summary"), config.now().isoformat(), row["key"]),
    )
    return {"issue_key": row["key"], "status": status}


def get_issue(args: dict[str, Any]) -> dict[str, Any]:
    row = _issue(args["issue_key"])
    comments = store.rows("select body, created_at from case_comment where issue_key = ? order by id", (row["key"],))
    return {"issue_key": row["key"], "status": row["status"], "fields": {**dict(row), "comments": comments}}


def assign_issue(args: dict[str, Any]) -> dict[str, Any]:
    row = _issue(args["issue_key"])
    store.run(
        "update case_issue set assignee = ?, updated_at = ? where key = ?",
        (args["assignee"], config.now().isoformat(), row["key"]),
    )
    return {"issue_key": row["key"], "assignee": args["assignee"]}


def add_comment(args: dict[str, Any]) -> dict[str, Any]:
    row = _issue(args["issue_key"])
    store.run(
        "insert into case_comment (issue_key, body, created_at) values (?, ?, ?)",
        (row["key"], args["body"], config.now().isoformat()),
    )
    cid = store.first("select last_insert_rowid() as id")
    return {"comment_id": str(cid["id"])}


def find_open_case(instance_id: str | None = None, summary_contains: str | None = None) -> dict[str, Any] | None:
    """Simulator-side helper for the thread rule: the open case of an instance or a subject."""
    if instance_id:
        row = store.one(
            "select * from case_issue where instance_id = ? and status != 'closed' order by created_at desc",
            (instance_id,),
        )
    else:
        row = store.one(
            "select * from case_issue where summary like ? and status != 'closed' order by created_at desc",
            (f"%{summary_contains}%",),
        )
    return dict(row) if row else None


def register() -> None:
    for tid, name, fn, se, idem, perms in [
        ("jira.create_issue", "Create issue", create_issue, "write", False, ("jira.issue.create",)),
        ("jira.update_issue", "Update issue", update_issue, "write", True, ("jira.issue.edit",)),
        ("jira.get_issue", "Retrieve issue status", get_issue, "read", True, ("jira.issue.read",)),
        ("jira.assign_issue", "Assign issue", assign_issue, "write", True, ("jira.issue.edit",)),
        ("jira.add_comment", "Add comment", add_comment, "write", False, ("jira.issue.comment",)),
    ]:
        registry.register(
            ToolSpec(id=tid, name=name, integration="jira", side_effect=se, idempotent=idem, permissions=perms), fn
        )
