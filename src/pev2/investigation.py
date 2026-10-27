"""Uncovered cases: the investigator, its guardrails, the discoveries log and the gap report (the loopback).

Three rules hold here, in code, not in a prompt:
  1. The investigator only reads. Its allow-list holds the read tools; anything else it asks for is refused by the
     registry, recorded, and becomes a discovery of kind "permission".
  2. A proposal never executes. `investigation_safe` marks each proposed action as supported or not; the only things
     that happen afterwards are a case for a person and a review the person decides.
  3. Everything the investigator found missing is written to `wf_discovery`, fingerprinted so the same gap seen ten
     times counts as one with ten occurrences, and exported as the gap report the studio reads to model the new path.
"""

from __future__ import annotations

import hashlib
import re
from typing import Any

from pev2 import config, store
from pev2.engine.runtime import WORKFLOW_META
from pev2.gateway import run_agent
from pev2.observability import feed, record
from pev2.tools import registry

KINDS = ("tool", "workflow", "policy", "data", "permission")


def _fingerprint(kind: str, description: str) -> str:
    words = sorted(
        set(re.findall(r"[a-z0-9_.]+", description.lower())) - {"the", "a", "an", "of", "for", "and", "to", "with"}
    )
    return hashlib.sha1((kind + ":" + " ".join(words)).encode()).hexdigest()[:12]


def catalog() -> dict[str, Any]:
    """What the platform can do today, handed to the investigator as data."""
    return {
        "workflows": {name: meta["trigger"] for name, meta in WORKFLOW_META.items()},
        "tools": sorted(registry.specs),
    }


def investigation_safe(out: dict[str, Any]) -> list[tuple[bool, str]]:
    """The gate: no proposed action may name a tool outside the catalog as something a workflow would run now, and the
    recommendation must be one the platform knows. Unsupported actions are kept, flagged, never executed."""
    checks: list[tuple[bool, str]] = []
    for a in out.get("proposed_actions", []):
        tool = a.get("tool")
        a["supported"] = bool(tool) and tool in registry.specs
        if tool and tool not in registry.specs:
            a["note"] = "not a catalog tool: recorded as a discovery, not executable"
        if tool and tool in registry.specs and registry.specs[tool].side_effect != "read":
            a["note"] = "a write: only through a modelled workflow with its approval"
    checks.append(
        (
            out.get("recommendation") in ("route_to_workflow", "human_action", "retry", "abandon", "escalate"),
            f"unknown recommendation {out.get('recommendation')}",
        )
    )
    if out.get("recommendation") == "route_to_workflow":
        checks.append((out.get("route_to") in WORKFLOW_META, f"route_to names no workflow: {out.get('route_to')}"))
    return checks


def record_discoveries(instance_id: str | None, out: dict[str, Any]) -> list[dict[str, Any]]:
    """Write what the investigator found missing, plus every tool it asked for and was refused."""
    found: list[dict[str, Any]] = []
    for m in out.get("missing_capabilities", []):
        kind = m.get("kind") if m.get("kind") in KINDS else "data"
        found.append(
            {
                "kind": kind,
                "description": str(m.get("description", ""))[:300],
                "evidence": str(m.get("why_needed", ""))[:300],
            }
        )
    already = " ".join(str(m.get("description", "")) for m in out.get("missing_capabilities", []))
    for a in out.get("proposed_actions", []):
        if a.get("tool") and not a.get("supported") and a["tool"] not in already:
            found.append(
                {
                    "kind": "tool",
                    "description": f"{a['tool']} (proposed: {a.get('action', '')[:120]})",
                    "evidence": str(a.get("rationale", ""))[:300],
                }
            )
    if instance_id:
        for row in feed(instance_id, 500):
            if row["kind"] == "tool" and row["status"] == "refused" and row["step"] == "agent.investigator":
                found.append(
                    {
                        "kind": "permission",
                        "description": f"{row['component']} asked for by the investigator",
                        "evidence": "refused by the allow-list",
                    }
                )
    now = config.now().isoformat()
    for d in found:
        d["fingerprint"] = _fingerprint(d["kind"], d["description"])
        store.run(
            "insert into wf_discovery (instance_id, kind, description, fingerprint, evidence, status, created_at) values (?, ?, ?, ?, ?, 'open', ?)",
            (instance_id, d["kind"], d["description"], d["fingerprint"], d["evidence"], now),
        )
    if found:
        record(
            instance_id,
            "Record discoveries",
            "discovery",
            "investigation",
            "recorded",
            {"count": len(found), "kinds": sorted({d["kind"] for d in found})},
        )
    return found


def investigate_message(
    sender: str, message: str, instance_id: str | None, facts: dict[str, Any] | None = None
) -> dict[str, Any]:
    supplier = store.one("select id, name, payment_terms_days from sap_supplier where email = ?", (sender,))
    inputs = {
        "sender": sender,
        "message": message,
        "supplier": dict(supplier) if supplier else None,
        "facts": facts or {},
        "catalog": catalog(),
        "process_instance_id": instance_id,
    }
    return run_agent(
        "agent.investigator", inputs, instance_id, "Work this uncovered message: facts, proposal, missing capabilities"
    )


def investigate_instance(iid: str) -> dict[str, Any]:
    """A side investigation of a parked instance: not a workflow step, so it never changes the instance's state."""
    inst = store.one("select * from wf_instance where id = ?", (iid,))
    if inst is None:
        raise KeyError(iid)
    trigger = store.loads(inst["trigger"])
    state = store.loads(inst["state"])
    facts: dict[str, Any] = {}
    for key, value in state.items():
        if key.startswith("step:") and isinstance(value, dict):
            for k in ("po_id", "invoice_id", "supplier_id", "po_number"):
                if isinstance(value.get(k), str):
                    facts[k] = value[k]
            inv = value.get("invoice")
            if isinstance(inv, dict):
                for k in ("po_id", "supplier_id"):
                    if inv.get(k):
                        facts[k] = inv[k]
    trail = [f"{a['kind']} {a['step']} → {a['status']}" for a in reversed(feed(iid, 60))]
    inputs = {
        "workflow": inst["workflow"],
        "trigger": trigger,
        "park_reason": inst["park_reason"] or "",
        "current_step": inst["current_step"],
        "facts": facts,
        "audit_trail": trail,
        "catalog": catalog(),
        "process_instance_id": iid,
    }
    out = run_agent(
        "agent.investigator", inputs, iid, "Work this parked instance: facts, proposal, missing capabilities"
    )
    gate = investigation_safe(out)
    out["gate"] = [reason for ok, reason in gate if not ok]
    out["discoveries"] = record_discoveries(iid, out)
    record(
        iid,
        "Investigate parked instance",
        "agent",
        "agent.investigator",
        "proposal",
        {"recommendation": out["recommendation"], "summary": out["summary"][:200]},
    )
    return out


def discoveries(status: str = "open") -> list[dict[str, Any]]:
    """Grouped by fingerprint: one row per distinct gap with how often and where it was seen."""
    rows = store.rows(
        "select fingerprint, kind, min(description) as description, min(evidence) as evidence, count(*) as seen, group_concat(distinct instance_id) as instances, min(created_at) as first_seen, max(created_at) as last_seen, min(status) as status from wf_discovery where (? = 'all' or status = ?) group by fingerprint order by seen desc, last_seen desc",
        (status, status),
    )
    return rows


def set_discovery_status(fingerprint: str, status: str) -> None:
    store.run("update wf_discovery set status = ? where fingerprint = ?", (status, fingerprint))


def gap_report() -> str:
    """The loopback document: what the platform could not do, grouped, with a scenario stub per workflow gap for the
    Agent Architecture Studio."""
    groups = discoveries("all")
    lines = [
        "# Gap report — what the platform could not handle",
        "",
        f"Generated {config.now().isoformat()[:16]} from {sum(g['seen'] for g in groups)} discoveries in {len(groups)} distinct gaps. Each gap is a candidate change for the architecture document; a workflow gap is a new scenario for the studio.",
        "",
    ]
    for kind in KINDS:
        of_kind = [g for g in groups if g["kind"] == kind]
        if not of_kind:
            continue
        lines += [f"## Missing {kind}s", "", "| seen | gap | why | status | instances |", "|---|---|---|---|---|"]
        for g in of_kind:
            lines.append(
                f"| {g['seen']} | {g['description']} | {g['evidence']} | {g['status']} | {(g['instances'] or '').replace(',', ', ')} |"
            )
        lines.append("")
    stubs = [g for g in groups if g["kind"] == "workflow"]
    if stubs:
        lines += ["## Scenario stubs for the studio", ""]
        for g in stubs:
            lines += [
                f"### {g['description']}",
                "",
                f"- Trigger: the situation seen {g['seen']} time(s) above",
                f"- Why: {g['evidence']}",
                "- Steps: to be designed; the investigator's proposals in the instances' audit trails are the first draft",
                "- Approvals and limits: to be decided by the process owner",
                "",
            ]
    return "\n".join(lines)
