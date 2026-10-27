"""The uncovered case: a message no workflow claims. Classify; route when a workflow covers it; otherwise let the
investigator establish the facts and propose, gate the proposal, record what is missing, open a case, and wait for
the person who decides. Nothing in this workflow writes to SAP."""

from __future__ import annotations

from typing import Any

from pev2.engine import Context, workflow
from pev2.investigation import investigate_message, investigation_safe, record_discoveries
from pev2.services import enquiries
from pev2.services.common import Tools


def _t(ctx: Context, service: str, role: str = "workflow") -> Tools:
    return Tools(caller=service, role=role, instance_id=ctx.instance_id)


@workflow(
    "unknown_case",
    title="A message no workflow covers",
    trigger="Unmatched message received",
    end_states=["Routed to a workflow", "Resolved by human"],
)
def unknown_case(ctx: Context) -> str:
    message, sender = ctx.trigger["message"], ctx.trigger.get("sender", "")
    # The keyword classifier is a hint in the data, never the router: "update our bank account before the next
    # payment run" contains "payment" and is not a payment question. The investigator decides what covers the case.
    ctx.step(
        "Classify message", lambda: {"kind": enquiries.classify_message(message)}, component="service.classify_message"
    )
    proposal = ctx.step(
        "Investigate",
        lambda: investigate_message(sender, message, ctx.instance_id),
        component="agent.investigator",
        kind="agent",
    )
    if proposal.get("recommendation") == "route_to_workflow" and proposal.get("route_to"):
        from pev2.app import runtime

        target = str(proposal["route_to"])
        routed = ctx.step(
            "Route to the covering workflow",
            lambda: {"workflow": target, "instance_id": runtime.start(target, {"message": message, "sender": sender})},
            component="engine.route",
        )
        ctx.state["routed_to"] = routed
        return "Routed to a workflow"
    ctx.gate("Proposal is safe", investigation_safe(proposal))
    ctx.step(
        "Record discoveries",
        lambda: {"discoveries": record_discoveries(ctx.instance_id, proposal)},
        component="investigation.record_discoveries",
    )
    case = ctx.step(
        "Open case for a person",
        lambda: _t(ctx, "service.open_case")(
            "jira.create_issue",
            idempotency_key=ctx.idem("case"),
            summary=f"Uncovered case: {proposal['summary'][:90]}",
            description=_describe(proposal, sender, message),
            issue_type="uncovered_case",
            assignee=proposal.get("owner") or "AP Specialist",
            instance_id=ctx.instance_id,
        ),
        component="tool.jira.create_issue",
        kind="tool",
    )
    ctx.review(
        "Investigator proposal",
        f"{proposal['risk']} risk: {proposal['summary'][:160]}",
        {
            "case": case.get("key"),
            "owner": proposal.get("owner"),
            "hypothesis": proposal.get("hypothesis"),
            "proposed_actions": proposal.get("proposed_actions"),
            "missing_capabilities": proposal.get("missing_capabilities"),
            "findings": proposal.get("findings"),
        },
    )
    return "Resolved by human"


def _describe(p: dict[str, Any], sender: str, message: str) -> str:
    acts = "\n".join(
        f"- [{'supported' if a.get('supported') else 'NOT SUPPORTED'}] {a.get('who')}: {a.get('action')}"
        for a in p.get("proposed_actions", [])
    )
    gaps = "\n".join(f"- {m.get('kind')}: {m.get('description')}" for m in p.get("missing_capabilities", []))
    return f"From: {sender}\n\n{message}\n\nInvestigator: {p['summary']}\nHypothesis: {p.get('hypothesis') or '-'}\nRisk: {p.get('risk')}\n\nProposed:\n{acts or '-'}\n\nMissing on the platform:\n{gaps or '-'}"
