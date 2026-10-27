"""The uncovered case: the investigator proposes, the gates hold, the discoveries land in the loopback."""

from __future__ import annotations

from fastapi.testclient import TestClient

from pev2 import store
from pev2.investigation import discoveries, gap_report, investigate_instance, investigation_safe

BANK = "We have changed our bank account to DK12 3456 7890, please update before the next payment run. Urgent."


def _sap_writes(iid: str) -> list[str]:
    return [
        r["component"]
        for r in store.rows(
            "select component from wf_audit where instance_id=? and kind='tool' and component like 'sap.%' and detail like '%\"side_effect\": \"write\"%'",
            (iid,),
        )
    ]


def test_bank_change_mail_is_investigated_not_executed(rt):
    iid = rt.start("unknown_case", {"sender": "ap@danfilm-plast.example", "message": BANK})
    inst = rt.instance(iid)
    assert inst["status"] == "parked" and inst["current_step"] == "Investigator proposal"
    review = rt.reviews()[0]
    assert review["instance_id"] == iid and "high risk" in review["reason"]
    actions = review["payload"]["proposed_actions"]
    assert any(a["tool"] == "sap.update_supplier_bank_account" and a["supported"] is False for a in actions), (
        "an unsupported action is flagged, never run"
    )
    assert _sap_writes(iid) == []
    case = store.one("select assignee, status from case_issue where issue_type='uncovered_case'")
    assert case["assignee"] == "Supplier Manager" and case["status"] == "open"
    kinds = {d["kind"] for d in discoveries()}
    assert {"tool", "workflow"} <= kinds
    rt.decide_review(review["id"], "retry", "verified by phone, genuine", "Noah")
    assert rt.instance(iid)["end_state"] == "Resolved by human"


def test_covered_message_is_routed_to_its_workflow(rt):
    iid = rt.start(
        "unknown_case", {"sender": "ap@danfilm-plast.example", "message": "When will invoice 2026-0912 be paid?"}
    )
    inst = rt.instance(iid)
    assert inst["end_state"] == "Routed to a workflow" and inst["state"]["routed_to"]["workflow"] == "supplier_query"
    routed = rt.instance(inst["state"]["routed_to"]["instance_id"])
    assert routed["end_state"] == "Enquiry answered"
    assert discoveries() == [], "a covered case is not a gap"


def test_keyword_hit_does_not_route_a_bank_change(rt):
    """'payment run' in the mail must not make it a payment question (the regex is a hint, the investigator decides)."""
    iid = rt.start("unknown_case", {"sender": "ap@danfilm-plast.example", "message": BANK})
    assert rt.instance(iid)["status"] == "parked"
    assert store.one("select count(*) as n from mail_message where subject like 'Your payment%'")["n"] == 0


def test_same_gap_counts_once_with_occurrences(rt):
    for _ in range(3):
        rt.start("unknown_case", {"sender": "ap@danfilm-plast.example", "message": BANK})
    rows = discoveries()
    workflow_gaps = [d for d in rows if d["kind"] == "workflow"]
    assert len(workflow_gaps) == 1 and workflow_gaps[0]["seen"] == 3
    report = gap_report()
    assert (
        "## Scenario stubs for the studio" in report and "Supplier master data change" in report and "| 3 |" in report
    )


def test_investigating_a_parked_instance_leaves_it_untouched(rt):
    store.run("delete from sap_goods_receipt where po_id = 'PO45088'")
    iid = rt.start("price_deviation", {"invoice_id": "INV7731"})
    before = rt.instance(iid)
    out = investigate_instance(iid)
    assert out["recommendation"] == "retry" and any(d["kind"] == "tool" for d in out["discoveries"])
    after = rt.instance(iid)
    assert after["status"] == before["status"] == "parked" and after["state"] == before["state"]
    assert _sap_writes(iid) == []


def test_investigator_holds_only_read_tools(world):
    from pev2.agents import AGENTS
    from pev2.tools import registry

    spec = AGENTS["agent.investigator"]
    assert spec.tools and all(registry.specs[t].side_effect == "read" for t in spec.tools)


def test_gate_flags_unknown_tools_and_recommendations():
    out = {
        "recommendation": "launch_rocket",
        "proposed_actions": [
            {"action": "x", "tool": "sap.delete_everything"},
            {"action": "y", "tool": "sap.get_invoice"},
            {"action": "z", "tool": "sap.release_invoice"},
        ],
    }
    checks = investigation_safe(out)
    assert any(not ok for ok, _ in checks)
    assert (
        out["proposed_actions"][0]["supported"] is False and "not a catalog tool" in out["proposed_actions"][0]["note"]
    )
    assert out["proposed_actions"][1]["supported"] is True
    assert "approval" in out["proposed_actions"][2]["note"]


def test_api_discoveries_round_trip(world):
    from pev2.api import app

    with TestClient(app) as c:
        r = c.post(
            "/api/instances",
            json={"workflow": "unknown_case", "trigger": {"sender": "ap@danfilm-plast.example", "message": BANK}},
        )
        assert r.json()["status"] == "parked"
        rows = c.get("/api/discoveries").json()
        assert rows and {d["kind"] for d in rows} >= {"tool", "workflow"}
        fp = rows[0]["fingerprint"]
        assert (
            c.post(f"/api/discoveries/{fp}", json={"decision": "accepted", "by": "Jan"}).json()["status"] == "accepted"
        )
        assert "Gap report" in c.get("/api/discoveries/report").text


def test_structured_gap_report_carries_cases_and_catalog(rt):
    from pev2.investigation import gap_report_json

    rt.start("unknown_case", {"sender": "ap@danfilm-plast.example", "message": BANK})
    report = gap_report_json()
    assert report["source"] == "ProcurementEngineV2" and report["gaps"] and report["cases"]
    case = report["cases"][0]
    assert case["owner"] == "Supplier Manager" and case["proposed_actions"] and case["trigger"]["message"] == BANK
    assert "supplier_query" in report["catalog"]["workflows"] and "sap.get_supplier" in report["catalog"]["tools"]
    assert all(isinstance(g["instances"], list) for g in report["gaps"])


def test_send_to_studio_posts_the_report(rt, monkeypatch):
    import json

    from pev2 import investigation

    rt.start("unknown_case", {"sender": "ap@danfilm-plast.example", "message": BANK})
    seen: dict[str, object] = {}

    class _Resp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return None

        def read(self):
            return json.dumps({"scenarios": [{"name": "Supplier master data change"}], "gaps": 2, "cases": 1}).encode()

    def fake_urlopen(req, timeout=0):
        seen["url"] = req.full_url
        seen["user"] = req.get_header("X-user")
        seen["body"] = json.loads(req.data.decode())
        return _Resp()

    monkeypatch.setattr(investigation.urllib.request, "urlopen", fake_urlopen)
    result = investigation.send_to_studio("proj1", "http://studio.example", "jan")
    assert (
        seen["url"] == "http://studio.example/api/projects/proj1/scenarios/import-gap-report" and seen["user"] == "jan"
    )
    assert seen["body"]["report"]["gaps"] and result["scenarios"][0]["name"].startswith("Supplier")
