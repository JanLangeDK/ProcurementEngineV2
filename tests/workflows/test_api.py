from __future__ import annotations

from fastapi.testclient import TestClient


def test_api_round_trip(world):
    from pev2.api import app

    with TestClient(app) as c:
        assert c.get("/api/health").json()["status"] == "ok"
        r = c.post("/api/instances", json={"workflow": "price_deviation", "trigger": {"invoice_id": "INV7731"}})
        assert r.status_code == 201
        iid = r.json()["id"]
        ap = c.get("/api/approvals").json()[0]
        assert (
            c.post(f"/api/approvals/{ap['id']}", json={"decision": "approved", "by": "Sofie"}).json()["status"]
            == "waiting_event"
        )
        assert c.post(
            "/api/events",
            json={
                "kind": "supplier_reply",
                "correlation": "inv:INV7731",
                "payload": {"body": "credit note CN-2026-118 follows", "from_addr": "ap@krydderihuset.example"},
            },
        ).json()["woken"] == [iid]
        ap = c.get("/api/approvals").json()[0]
        c.post(f"/api/approvals/{ap['id']}", json={"decision": "approved", "by": "Mette"})
        inst = c.get(f"/api/instances/{iid}").json()
        assert inst["end_state"] == "Invoice released for payment" and len(inst["audit"]) > 10
        assert c.get("/api/sap/invoices/INV7731").json()["status"] == "posted"
        assert c.post("/api/reset").json()["status"] == "reset"
