"""Phase 0: the engine persists, replays and survives a restart; tools have schemas, mocks and idempotency; approvals work."""

from __future__ import annotations

import pytest

from pev2 import store
from pev2.engine.runtime import Context, Runtime, Suspend, workflow
from pev2.errors import LimitError, TransientError
from pev2.tools import registry


def test_every_tool_is_registered_with_side_effect_and_permissions(world):
    assert len(registry.specs) >= 29
    for spec in registry.specs.values():
        assert spec.side_effect in ("read", "write", "notify")
        assert spec.permissions, spec.id
    assert registry.specs["sap.release_invoice"].side_effect == "write"
    assert registry.specs["email.send_approved"].side_effect == "notify"


def test_write_tools_need_an_idempotency_key_and_replay_on_repeat(world):
    registry.permit("test.caller", ["jira.create_issue"])
    with pytest.raises(LimitError):
        registry.call("jira.create_issue", {"summary": "x"}, caller="test.caller", role="workflow")
    first = registry.call(
        "jira.create_issue", {"summary": "x"}, caller="test.caller", role="workflow", idempotency_key="k1"
    )
    again = registry.call(
        "jira.create_issue", {"summary": "x"}, caller="test.caller", role="workflow", idempotency_key="k1"
    )
    assert first == again
    assert store.one("select count(*) as n from case_issue")["n"] == 1


def test_allow_list_is_enforced(world):
    registry.permit("test.narrow", ["sap.get_invoice"])
    with pytest.raises(LimitError):
        registry.call(
            "sap.block_invoice",
            {"document_number": "INV7731"},
            caller="test.narrow",
            role="ap_specialist",
            idempotency_key="k",
        )


def test_agents_never_hold_financial_write_or_send_tools(world):
    from pev2.agents import AGENTS

    for spec in AGENTS.values():
        for tid in spec.tools:
            t = registry.specs[tid]
            assert t.side_effect != "notify", spec.id
            assert not (t.side_effect == "write" and t.integration == "sap"), spec.id


def test_outage_exhausts_retries_and_parks(world):
    registry.permit("test.caller", ["sap.get_invoice"])
    registry.outages = {"sap"}
    with pytest.raises(TransientError):
        registry.call("sap.get_invoice", {"invoice_id": "INV7731"}, caller="test.caller")
    attempts = store.rows("select * from wf_audit where status = 'transient_error'")
    assert len(attempts) == 3


def test_workflow_persists_and_survives_restart(world):
    @workflow("t_wait", title="t", trigger="t", end_states=["done"])
    def wf(ctx: Context) -> str:
        ctx.step("one", lambda: {"v": 1})
        ctx.wait_event("Wait for: thing", "thing", "c1")
        ctx.step("two", lambda: {"v": 2})
        return "done"

    rt = Runtime()
    iid = rt.start("t_wait", {})
    assert rt.instance(iid)["status"] == "waiting_event"
    fresh = Runtime()  # a 'restart': nothing in memory, everything in the file
    fresh.deliver_event("thing", "c1", {"x": 1})
    inst = fresh.instance(iid)
    assert inst["status"] == "completed" and inst["end_state"] == "done"
    assert inst["state"]["step:one"] == {"v": 1} and inst["state"]["event:Wait for: thing"] == {"x": 1}
    steps = [
        a["step"]
        for a in store.rows(
            "select step from wf_audit where instance_id = ? and kind = 'service' and status = 'started'", (iid,)
        )
    ]
    assert steps.count("one") == 1, "a replayed step must not execute again"


def test_approval_requested_listed_decided_and_resumed(world):
    @workflow("t_appr", title="t", trigger="t", end_states=["ok", "no"])
    def wf(ctx: Context) -> str:
        d = ctx.approval("Say yes", "AP Team Lead", {"q": 1})
        return "ok" if d["decision"] == "approved" else "no"

    rt = Runtime()
    iid = rt.start("t_appr", {})
    assert rt.instance(iid)["status"] == "waiting_approval"
    pending = rt.approvals()
    assert len(pending) == 1 and pending[0]["owner"] == "AP Team Lead"
    with pytest.raises(ValueError):
        rt.decide_approval(pending[0]["id"], "maybe", "x")
    rt.decide_approval(pending[0]["id"], "rejected", "Mette")
    assert rt.instance(iid)["end_state"] == "no"
    with pytest.raises(ValueError):
        rt.decide_approval(pending[0]["id"], "approved", "x")


def test_tick_escalates_overdue_approvals(world, monkeypatch):
    @workflow("t_esc", title="t", trigger="t", end_states=["ok"])
    def wf(ctx: Context) -> str:
        ctx.approval("Decide", "AP Team Lead", {}, timeout_days=2, escalation="Finance Manager")
        return "ok"

    rt = Runtime()
    rt.start("t_esc", {})
    assert rt.tick() == []
    monkeypatch.setenv("PEV2_CLOCK", "2026-10-25T08:00:00Z")
    actions = rt.tick()
    assert actions and actions[0]["escalated_to"] == "Finance Manager"
    assert "Finance Manager" in rt.approvals()[0]["owner"]


def test_suspend_is_not_an_error(world):
    with pytest.raises(Suspend):
        raise Suspend("waiting_event", "x")
