"""The window for testing everything: Gradio, seven tabs.

Simulator  fire the events that start workflows (an invoice arrives, a payment question, a component requirement,
           a late delivery) and the events workflows wait for (supplier replies, confirmations).
Instances  every workflow instance, its status, the step it is at, and its audit trail.
Approvals  the inbox: pick who you are (AP Specialist, AP Team Lead, Procurement Manager ...), approve or reject.
Reviews    parked instances waiting for a human (exceptions, failed gates, outages): retry or abandon.
Cases & mail  the simulated case system and mailbox (drafts, sent, inbound).
SAP-sim    invoices and purchase orders as the simulated SAP sees them.
Policy     the invoice-handling policy document; edit the tolerance and the next run behaves differently.

All logic is in plain functions so it is testable without a browser; `build_app` only wires components.
"""

from __future__ import annotations

import json
from typing import Any

import gradio as gr

from pev2 import config, store
from pev2.app import ready, runtime
from pev2.investigation import discoveries, gap_report, investigate_instance, set_discovery_status
from pev2.observability import feed
from pev2.tools import brain, email, sap

PERSONAS = [
    "AP Specialist (Sofie)",
    "AP Team Lead (Mette)",
    "Procurement Manager (Henrik)",
    "Supplier Manager (Noah)",
    "Finance Manager",
    "Head of Procurement",
]
INVOICES = [
    "INV7731 · S1004 Krydderihuset · price deviation on M2110",
    "INV7701 · S1014 · clean invoice",
    "INV7732 · S1009 Jysk Rengøringskemi · received",
    "INV7702 · S1002 Danfilm · posted (duplicate check)",
]
REQUIREMENTS = {
    "2000 rolls M2001 packaging film, Horsens, 30 Oct (story A)": {
        "material_id": "M2001",
        "quantity": 2000,
        "required_date": "2026-10-30",
        "requester": "Lars Bak",
        "plant": "Horsens",
    },
    "10 pcs M2007, Ringsted (covered by stock)": {
        "material_id": "M2007",
        "quantity": 10,
        "required_date": "2026-11-30",
        "requester": "Anette Krogh",
        "plant": "Ringsted",
    },
    "900 pcs M2201 cleaning chemicals, Ringsted, 20 Nov (above buyer limit)": {
        "material_id": "M2201",
        "quantity": 900,
        "required_date": "2026-11-20",
        "requester": "Anette Krogh",
        "plant": "Ringsted",
    },
    "Unknown material M9999": {
        "material_id": "M9999",
        "quantity": 10,
        "required_date": "2026-11-30",
        "requester": "Anette Krogh",
        "plant": "Ringsted",
    },
}
SUPPLIER_SENDERS = {
    "Danfilm Plast A/S (S1002)": "ap@danfilm-plast.example",
    "Krydderihuset ApS (S1004)": "ap@krydderihuset.example",
    "Jysk Rengøringskemi ApS (S1009)": "ap@jysk-rengoeringskemi.example",
    "Unknown sender": "someone@example.com",
}


def _status_line(iid: str) -> str:
    i = runtime.instance(iid)
    if not i:
        return "?"
    tail = f" → **{i['end_state']}**" if i["end_state"] else (f" — {i['park_reason']}" if i["park_reason"] else "")
    return f"Instance `{iid}` ({i['workflow']}): **{i['status']}** at *{i['current_step']}*{tail}"


# ---- simulator ------------------------------------------------------------------------------------------
def fire_invoice(choice: str, workflow: str) -> str:
    inv = choice.split(" ")[0]
    sender = {"INV7731": "ap@krydderihuset.example", "INV7732": "ap@jysk-rengoeringskemi.example"}.get(inv, "edi")
    iid = runtime.start(
        "price_deviation" if workflow.startswith("Price") else "invoice_processing",
        {"invoice_id": inv, "sender": sender},
    )
    return _status_line(iid)


def fire_payment_question(sender_label: str, text: str) -> str:
    sender = SUPPLIER_SENDERS[sender_label]
    email.inbound(sender, "Payment question", text)
    iid = runtime.start("supplier_query", {"message": text, "sender": sender})
    return _status_line(iid)


def fire_requirement(choice: str) -> str:
    iid = runtime.start("production_purchase", REQUIREMENTS[choice])
    return _status_line(iid)


def supplier_reply(sender_label: str, invoice_id: str, text: str) -> str:
    sender = SUPPLIER_SENDERS[sender_label]
    thread = f"inv:{invoice_id.strip()}"
    mid = email.inbound(sender, f"Re: invoice {invoice_id}", text, thread_id=thread)
    woken = runtime.deliver_event("supplier_reply", thread, {"body": text, "from_addr": sender, "id": mid})
    return (
        "\n\n".join(_status_line(i) for i in woken)
        if woken
        else f"Reply recorded on thread {thread}; no instance was waiting for it."
    )


def supplier_confirmation(po_number: str, status: str, date: str, note: str) -> str:
    po = po_number.strip()
    sap.post_confirmation(po, status, date.strip() or None, note)
    woken = runtime.deliver_event("supplier_confirmation", po, {"status": status, "confirmed_date": date})
    return (
        "\n\n".join(_status_line(i) for i in woken)
        if woken
        else f"Confirmation recorded for {po}; no instance was waiting for it."
    )


UNCOVERED = {
    "Bank account change (fraud pattern)": "We have changed our bank account to DK12 3456 7890 1234, please update before the next payment run. Urgent, regards, Danfilm accounting.",
    "Contract renewal question": "Our framework agreement C-2026-014 expires soon. Can we renew at the same prices for two more years?",
    "Quality complaint about a delivery": "The last delivery of M2001 arrived damaged; 40 rolls are unusable. How do we handle the return and the invoice?",
    "A payment question (covered, gets routed)": "When will invoice 2026-0912 be paid?",
}


def fire_uncovered(sender_label: str, choice: str, text: str) -> str:
    sender = SUPPLIER_SENDERS[sender_label]
    body = text.strip() or UNCOVERED[choice]
    email.inbound(sender, "Message", body)
    iid = runtime.start("unknown_case", {"message": body, "sender": sender})
    return _status_line(iid)


def tick() -> str:
    actions = runtime.tick()
    return f"Scheduler tick at {config.now().isoformat()}: {actions or 'nothing due'}"


def do_reset() -> str:
    store.reset()
    return "Reset: runtime emptied, SAP-sim, cases and mail reloaded from fixtures."


# ---- instances ---------------------------------------------------------------------------------------------
INSTANCE_HEADERS = ["id", "workflow", "status", "current step", "end state", "parked because", "updated"]


def instance_rows() -> list[list[Any]]:
    return [
        [
            i["id"],
            i["workflow"],
            i["status"],
            i["current_step"],
            i["end_state"] or "",
            (i["park_reason"] or "")[:120],
            i["updated_at"][:19],
        ]
        for i in runtime.instances()
    ]


def instance_choices() -> list[str]:
    return [f"{i['id']} · {i['workflow']} · {i['status']}" for i in runtime.instances()]


def instance_detail(choice: str | None) -> str:
    if not choice:
        return "Pick an instance."
    iid = choice.split(" ·")[0].strip()
    i = runtime.instance(iid)
    if not i:
        return "?"
    head = f"### {i['workflow']} · `{iid}` · **{i['status']}**" + (f" → {i['end_state']}" if i["end_state"] else "")
    lines = [head, f"Trigger: `{json.dumps(i['trigger'], ensure_ascii=False)}`"]
    if i["park_reason"]:
        lines.append(f"Parked: {i['park_reason']}")
    lines += ["", "| at | kind | step | component | status | detail |", "|---|---|---|---|---|---|"]
    for a in reversed(feed(iid, 400)):
        detail = (a["detail"] or "")[:160].replace("|", "/").replace("\n", " ")
        lines.append(f"| {a['at'][11:19]} | {a['kind']} | {a['step']} | {a['component']} | {a['status']} | {detail} |")
    return "\n".join(lines)


# ---- approvals -------------------------------------------------------------------------------------------
def approval_choices(persona: str) -> list[str]:
    who = persona.split(" (")[0]
    out = []
    for a in runtime.approvals():
        mine = (
            who.lower() in a["owner"].lower() or "escalated" in a["owner"].lower() and who.lower() in a["owner"].lower()
        )
        out.append(f"{'★ ' if mine else '  '}{a['id']} · {a['name']} · for {a['owner']} · instance {a['instance_id']}")
    return out


def approval_detail(choice: str | None) -> str:
    if not choice:
        return "Pick an approval."
    aid = choice.strip("★ ").split(" ·")[0].strip()
    row = next((a for a in runtime.approvals() if a["id"] == aid), None)
    if row is None:
        return "Already decided."
    facts = "\n".join(
        f"- **{k}**: {v if not isinstance(v, (dict, list)) else json.dumps(v, ensure_ascii=False)}"
        for k, v in row["payload"].items()
    )
    return f"### {row['name']}\nOwner: **{row['owner']}** · requested {row['requested_at'][:16]} · due {row['due_at'][:16]} · escalates to {row['escalated_to'] or '—'}\n\n{facts}"


def decide(choice: str | None, persona: str, decision: str, note: str) -> str:
    if not choice:
        return "Pick an approval first."
    aid = choice.strip("★ ").split(" ·")[0].strip()
    row = next((a for a in runtime.approvals() if a["id"] == aid), None)
    if row is None:
        return "Already decided."
    who = persona.split(" (")[0]
    if who.lower() not in row["owner"].lower():
        return f"Segregation of duties: this approval belongs to **{row['owner']}**, you are {persona}."
    inst = runtime.decide_approval(aid, decision, persona, note)
    return f"{decision} by {persona}. " + _status_line(inst["id"])


# ---- reviews -------------------------------------------------------------------------------------------------
def review_choices() -> list[str]:
    return [f"{r['id']} · {r['step']} · {r['reason'][:90]} · instance {r['instance_id']}" for r in runtime.reviews()]


def review_decide(choice: str | None, decision: str, note: str, persona: str) -> str:
    if not choice:
        return "Pick a review item first."
    rid = int(choice.split(" ·")[0])
    inst = runtime.decide_review(rid, "retry" if decision == "Retry the step" else "abandon", note, persona)
    return _status_line(inst["id"])


def proposal_markdown(out: dict[str, Any]) -> str:
    acts = "\n".join(
        f"- {'OK' if a.get('supported') else 'NOT SUPPORTED'} **{a.get('who')}**: {a.get('action')}"
        + (f" — tool `{a['tool']}`" if a.get("tool") else "")
        + (f" *({a['note']})*" if a.get("note") else "")
        for a in out.get("proposed_actions", [])
    )
    gaps = "\n".join(
        f"- **{m.get('kind')}**: {m.get('description')} — {m.get('why_needed', '')}"
        for m in out.get("missing_capabilities", [])
    )
    facts = "\n".join(f"- {f}" for f in out.get("findings", []))
    return (
        f"### Investigator proposal · risk **{out.get('risk')}** · recommends **{out.get('recommendation')}** · owner {out.get('owner')}\n\n"
        f"{out.get('summary')}\n\n**Hypothesis:** {out.get('hypothesis') or '—'}\n\n**Findings**\n{facts or '- none'}\n\n"
        f"**Proposed actions** (nothing here executes; NOT SUPPORTED = the platform cannot do it today)\n{acts or '- none'}\n\n"
        f"**Missing on the platform** (recorded as discoveries)\n{gaps or '- none'}"
        + (f"\n\n*{out['rationale']}*" if out.get("rationale") else "")
    )


def review_investigate(choice: str | None) -> str:
    if not choice:
        return "Pick a review item first."
    rid = int(choice.split(" ·")[0])
    row = next((r for r in runtime.reviews() if r["id"] == rid), None)
    if row is None:
        return "Already decided."
    if row["step"] == "Investigator proposal":
        return proposal_markdown(
            row["payload"]
            | {
                "summary": row["reason"],
                "risk": row["reason"].split(" risk")[0],
                "recommendation": "human decides",
                "rationale": "",
                "findings": row["payload"].get("findings", []),
            }
        )
    return proposal_markdown(investigate_instance(row["instance_id"]))


DISCOVERY_HEADERS = ["seen", "kind", "gap", "why", "status", "first seen", "instances"]


def discovery_rows() -> list[list[Any]]:
    return [
        [d["seen"], d["kind"], d["description"], d["evidence"], d["status"], d["first_seen"][:16], d["instances"] or ""]
        for d in discoveries("all")
    ]


def discovery_choices() -> list[str]:
    return [f"{d['fingerprint']} · {d['kind']} · {d['description'][:80]}" for d in discoveries("all")]


def discovery_set(choice: str | None, status: str) -> str:
    if not choice:
        return "Pick a discovery."
    set_discovery_status(choice.split(" ·")[0], status)
    return f"Marked {status}."


def export_gap_report() -> str:
    text = gap_report()
    path = config.ROOT / "data" / "runtime" / "gap-report.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return f"Written to `{path}`; paste it into the studio as the brief for the next scenario.\n\n---\n\n" + text


def outages(sap_down: bool, email_down: bool, brain_down: bool, jira_down: bool) -> str:
    from pev2.tools import registry

    registry.outages = {
        name
        for name, down in (("sap", sap_down), ("email", email_down), ("brain", brain_down), ("jira", jira_down))
        if down
    }
    return f"Simulated outages: {sorted(registry.outages) or 'none'}"


# ---- cases, mail, sap --------------------------------------------------------------------------------------
def case_rows() -> list[list[Any]]:
    return [
        [c["key"], c["issue_type"], c["summary"], c["status"], c["assignee"] or "", c["instance_id"] or ""]
        for c in store.rows("select * from case_issue order by created_at desc")
    ]


def mail_rows() -> list[list[Any]]:
    return [
        [
            m["id"],
            m["direction"],
            m["status"],
            m["to_addr"] if m["direction"] == "out" else m["from_addr"],
            m["subject"],
            (m["body"] or "")[:140],
            m["approval_ref"] or "",
        ]
        for m in store.rows("select * from mail_message order by id desc")
    ]


def invoice_rows() -> list[list[Any]]:
    return [
        [
            i["id"],
            i["supplier_id"],
            i["po_id"],
            i["invoice_number"],
            i["amount"],
            i["status"],
            i["credit_note_ref"] or "",
            (i["note"] or "")[:80],
        ]
        for i in store.rows("select * from sap_invoice where status != 'paid' order by received_at desc limit 60")
    ]


def po_rows() -> list[list[Any]]:
    return [
        [
            p["id"],
            p["supplier_id"],
            p["status"],
            p["requester"],
            p["plant"],
            p["delivery_date"],
            p["confirmed_date"] or "",
            p["approval_ref"] or "",
        ]
        for p in store.rows(
            "select * from sap_purchase_order order by (created_by = 'workflow') desc, created_at desc limit 40"
        )
    ]


def audit_rows() -> list[list[Any]]:
    return [
        [a["at"][11:19], a["instance_id"] or "", a["kind"], a["step"], a["component"], a["status"]]
        for a in feed(limit=150)
    ]


def policy_text() -> str:
    return (brain.DOCS / "policies" / "invoice-handling.md").read_text(encoding="utf-8")


def save_policy(text: str) -> str:
    (brain.DOCS / "policies" / "invoice-handling.md").write_text(text, encoding="utf-8")
    facts = brain.parse_tolerance(text)
    return f"Saved. The matcher now reads: price tolerance {facts['pct']}% or {facts['abs']} {facts['currency']}, quantity tolerance {facts['quantity_tolerance']}. Fire INV7731 again to see the difference."


# ---- the app ------------------------------------------------------------------------------------------------
def build_app() -> gr.Blocks:
    ready()
    with gr.Blocks(title="Procurement Engine V2") as app:
        gr.Markdown(
            "# Procurement Engine V2\nOne durable workflow platform, deterministic services and tools, three bounded agents, every approval kept. Everything here runs on mocks; nothing talks to a real system."
        )
        with gr.Tabs():
            with gr.Tab("Simulator"):
                out = gr.Markdown()
                with gr.Row():
                    with gr.Column():
                        gr.Markdown(
                            "**An invoice arrives** (story B: INV7731 has DKK 52.00 against 48.00 in contract C-2026-021)"
                        )
                        inv = gr.Dropdown(INVOICES, value=INVOICES[0], label="Invoice")
                        wf = gr.Radio(
                            ["Price deviation workflow (story B)", "Invoice processing workflow (general)"],
                            value="Price deviation workflow (story B)",
                            label="Run it through",
                        )
                        gr.Button("Invoice received", variant="primary").click(fire_invoice, [inv, wf], out)
                    with gr.Column():
                        gr.Markdown("**A supplier asks when it gets paid** (story C)")
                        sender = gr.Dropdown(list(SUPPLIER_SENDERS), value="Danfilm Plast A/S (S1002)", label="From")
                        q = gr.Textbox(value="When will invoice 2026-0912 be paid?", label="Message")
                        gr.Button("Payment enquiry received", variant="primary").click(
                            fire_payment_question, [sender, q], out
                        )
                    with gr.Column():
                        gr.Markdown("**A component requirement** (story A)")
                        req = gr.Dropdown(list(REQUIREMENTS), value=list(REQUIREMENTS)[0], label="Requirement")
                        gr.Button("Component requirement identified", variant="primary").click(
                            fire_requirement, req, out
                        )
                with gr.Row():
                    with gr.Column(scale=1):
                        gr.Markdown(
                            "**Something no workflow covers** (the investigator reads it, proposes, and reports what is missing)"
                        )
                    with gr.Column(scale=3):
                        with gr.Row():
                            u_sender = gr.Dropdown(
                                list(SUPPLIER_SENDERS), value="Danfilm Plast A/S (S1002)", label="From"
                            )
                            u_choice = gr.Dropdown(list(UNCOVERED), value=list(UNCOVERED)[0], label="Example")
                        u_text = gr.Textbox(label="Or write your own message (leave empty to use the example)", lines=2)
                        gr.Button("Unmatched message received", variant="primary").click(
                            fire_uncovered, [u_sender, u_choice, u_text], out
                        )
                gr.Markdown("---\n**Events a workflow waits for**")
                with gr.Row():
                    with gr.Column():
                        r_sender = gr.Dropdown(
                            list(SUPPLIER_SENDERS), value="Krydderihuset ApS (S1004)", label="Supplier replies"
                        )
                        r_inv = gr.Textbox(value="INV7731", label="About invoice")
                        r_text = gr.Textbox(
                            value="Sorry, our mistake, credit note CN-2026-118 follows for the difference.",
                            label="Reply",
                        )
                        gr.Button("Supplier reply received").click(supplier_reply, [r_sender, r_inv, r_text], out)
                    with gr.Column():
                        c_po = gr.Textbox(label="PO number (see Instances → state, or SAP-sim)")
                        c_status = gr.Radio(
                            ["confirmed", "delayed", "rejected", "unavailable"], value="confirmed", label="Confirmation"
                        )
                        c_date = gr.Textbox(value="2026-10-29", label="Confirmed date")
                        c_note = gr.Textbox(label="Note")
                        gr.Button("Supplier confirmation received").click(
                            supplier_confirmation, [c_po, c_status, c_date, c_note], out
                        )
                with gr.Row():
                    gr.Button("Scheduler tick (escalate overdue approvals, time out waits)").click(tick, None, out)
                    gr.Button("Reset everything", variant="stop").click(do_reset, None, out)
                with gr.Accordion("Simulate outages (retry budget runs out, instance parks)", open=False):
                    with gr.Row():
                        o1, o2, o3, o4 = (
                            gr.Checkbox(label=n)
                            for n in ("SAP down", "Email down", "Knowledge base down", "Case management down")
                        )
                    outage_out = gr.Markdown()
                    for o in (o1, o2, o3, o4):
                        o.change(outages, [o1, o2, o3, o4], outage_out)
            with gr.Tab("Instances"):
                table = gr.Dataframe(value=instance_rows(), headers=INSTANCE_HEADERS, interactive=False, wrap=True)
                gr.Timer(2).tick(instance_rows, None, table)
                pick = gr.Dropdown(instance_choices(), label="Instance (audit trail)")
                gr.Timer(2).tick(lambda: gr.Dropdown(choices=instance_choices()), None, pick)
                detail = gr.Markdown("Pick an instance to see its audit trail.")
                pick.change(instance_detail, pick, detail)
            with gr.Tab("Approvals"):
                persona = gr.Dropdown(PERSONAS, value=PERSONAS[0], label="You are")
                with gr.Row():
                    with gr.Column():
                        pending = gr.Dropdown(approval_choices(PERSONAS[0]), label="Waiting for a decision (★ = yours)")
                        gr.Timer(2).tick(lambda p: gr.Dropdown(choices=approval_choices(p)), persona, pending)
                        persona.change(lambda p: gr.Dropdown(choices=approval_choices(p)), persona, pending)
                        note = gr.Textbox(label="Note")
                        with gr.Row():
                            approve = gr.Button("Approve", variant="primary")
                            reject = gr.Button("Reject", variant="stop")
                        decision_out = gr.Markdown()
                    with gr.Column():
                        ap_detail = gr.Markdown("Pick an approval.")
                        pending.change(approval_detail, pending, ap_detail)
                approve.click(lambda c, p, n: decide(c, p, "approved", n), [pending, persona, note], decision_out)
                reject.click(lambda c, p, n: decide(c, p, "rejected", n), [pending, persona, note], decision_out)
            with gr.Tab("Reviews"):
                gr.Markdown(
                    "Parked instances: a modelled exception without a path, a failed gate, low agent confidence, or an outage after the retry budget. A human decides."
                )
                rv = gr.Dropdown(review_choices(), label="Open reviews")
                gr.Timer(2).tick(lambda: gr.Dropdown(choices=review_choices()), None, rv)
                rv_decision = gr.Radio(
                    ["Retry the step", "Abandon the instance"], value="Retry the step", label="Decision"
                )
                rv_note = gr.Textbox(label="Note")
                rv_out = gr.Markdown()
                with gr.Row():
                    gr.Button("Decide", variant="primary").click(
                        review_decide, [rv, rv_decision, rv_note, persona], rv_out
                    )
                    rv_inv = gr.Button("Ask the investigator (read-only; proposes, never acts)")
                rv_proposal = gr.Markdown()
                rv_inv.click(review_investigate, rv, rv_proposal)
            with gr.Tab("Discoveries"):
                gr.Markdown(
                    "The loopback. Every capability the investigator found missing: tools, workflows, policies, data, permissions. Grouped, counted, exportable as the brief for the studio's next scenario."
                )
                dt = gr.Dataframe(value=discovery_rows(), headers=DISCOVERY_HEADERS, interactive=False, wrap=True)
                gr.Timer(3).tick(discovery_rows, None, dt)
                with gr.Row():
                    d_pick = gr.Dropdown(discovery_choices(), label="Discovery")
                    gr.Timer(3).tick(lambda: gr.Dropdown(choices=discovery_choices()), None, d_pick)
                    d_status = gr.Radio(["accepted", "dismissed", "open"], value="accepted", label="Mark as")
                    d_out = gr.Markdown()
                    gr.Button("Apply").click(discovery_set, [d_pick, d_status], d_out)
                g_out = gr.Markdown()
                gr.Button("Export gap report for the studio", variant="primary").click(export_gap_report, None, g_out)
            with gr.Tab("Cases & mail"):
                gr.Markdown("**Cases** (simulated case management)")
                ct = gr.Dataframe(
                    value=case_rows(),
                    headers=["key", "type", "summary", "status", "assignee", "instance"],
                    interactive=False,
                    wrap=True,
                )
                gr.Timer(2).tick(case_rows, None, ct)
                gr.Markdown("**Mailbox** (drafts, approved and sent, inbound)")
                mt = gr.Dataframe(
                    value=mail_rows(),
                    headers=["id", "dir", "status", "to/from", "subject", "body", "approval"],
                    interactive=False,
                    wrap=True,
                )
                gr.Timer(2).tick(mail_rows, None, mt)
            with gr.Tab("SAP-sim"):
                gr.Markdown("**Invoices**")
                it = gr.Dataframe(
                    value=invoice_rows(),
                    headers=["id", "supplier", "po", "supplier no.", "amount", "status", "credit note", "note"],
                    interactive=False,
                    wrap=True,
                )
                gr.Timer(3).tick(invoice_rows, None, it)
                gr.Markdown("**Purchase orders**")
                pt = gr.Dataframe(
                    value=po_rows(),
                    headers=["id", "supplier", "status", "requester", "plant", "due", "confirmed", "approval"],
                    interactive=False,
                    wrap=True,
                )
                gr.Timer(3).tick(po_rows, None, pt)
            with gr.Tab("Audit"):
                at = gr.Dataframe(
                    value=audit_rows(),
                    headers=["at", "instance", "kind", "step", "component", "status"],
                    interactive=False,
                    wrap=True,
                )
                gr.Timer(2).tick(audit_rows, None, at)
            with gr.Tab("Policy"):
                gr.Markdown(
                    "The invoice-handling policy the matcher reads (`fixtures/brain/policies/invoice-handling.md`). Change the tolerance, save, fire INV7731 again: the same code behaves differently."
                )
                pol = gr.Textbox(value=policy_text(), lines=24, label="invoice-handling.md")
                pol_out = gr.Markdown()
                gr.Button("Save policy", variant="primary").click(save_policy, pol, pol_out)
    assert isinstance(app, gr.Blocks)
    return app


def main(port: int = 7860) -> None:
    build_app().launch(server_name="0.0.0.0", server_port=port, show_error=True)
