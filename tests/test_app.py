import os, tempfile
os.environ["DB_PATH"] = os.path.join(tempfile.mkdtemp(), "c.db")
os.environ["SEED_DEMO"] = "1"
import pytest
from fastapi.testclient import TestClient
from app.main import app, db, seed_demo, search_kb

client = TestClient(app)


def as_(user):
    t = client.post("/api/login", json={"username": user, "password": "demo123"}).json()["token"]
    return {"Authorization": "Bearer " + t}


@pytest.fixture(autouse=True)
def clean():
    with db() as c:
        c.execute("delete from cases")


def new(subject, body="please help", who="asha"):
    return client.post("/api/cases", json={"customer": "Neha", "subject": subject, "body": body}, headers=as_(who)).json()


def run(cid, who="asha"):
    return client.post(f"/api/cases/{cid}/run", headers=as_(who))


def test_requires_login():
    assert client.get("/api/cases").status_code == 401


def test_pii_masked_before_storing():
    c = new("Help", "mail me at neha@acme.com or call 9876543210")
    assert "neha@acme.com" not in c["body"] and "[email]" in c["body"] and "[number]" in c["body"]


def test_agent_drafts_from_knowledge_base():
    r = run(new("Reset password", "I forgot my password and I am locked out")["id"]).json()
    assert r["status"] == "drafted" and "Forgot password" in r["reply"]
    assert [t["tool"] for t in r["trace"]] == ["set_priority", "search_kb", "draft_reply"]


def test_guardrail_escalates_sensitive_topics():
    r = run(new("Refund please", "I want a refund")["id"]).json()
    assert r["status"] == "escalated" and r["trace"][-1]["input"] == "policy guardrail"


def test_low_confidence_escalates():
    r = run(new("Roadmap", "when is the mobile app launch")["id"]).json()
    assert r["status"] == "escalated" and r["reply"] == ""


def test_high_priority_detected():
    assert run(new("Urgent outage", "slack integration channel notifications down for hours")["id"]).json()["priority"] == "high"


def test_agent_runs_only_once():
    cid = new("Reset password", "forgot password locked")["id"]
    run(cid)
    assert run(cid).status_code == 409


def test_only_manager_approves_drafts():
    cid = new("Reset password", "forgot password locked")["id"]
    run(cid)
    assert client.post(f"/api/cases/{cid}/approve", headers=as_("asha")).status_code == 403
    assert client.post(f"/api/cases/{cid}/approve", headers=as_("meera")).json()["status"] == "sent"
    assert client.post(f"/api/cases/{cid}/approve", headers=as_("meera")).status_code == 409


def test_not_found_and_validation():
    assert run(999).status_code == 404
    assert client.post("/api/cases", json={"customer": "", "subject": "x", "body": "y"}, headers=as_("asha")).status_code == 422


def test_stats_and_search():
    run(new("Reset password", "forgot password locked")["id"])
    run(new("Refund", "refund")["id"])
    s = client.get("/api/stats", headers=as_("asha")).json()
    assert s["drafted"] == 1 and s["escalated"] == 1 and s["auto_draft_rate"] == 50
    assert search_kb("export csv report")[1][0] == 3


def test_seed_and_index():
    seed_demo()
    seed_demo()
    assert len(client.get("/api/cases", headers=as_("meera")).json()) == 6
    assert "CaseCopilot" in client.get("/").text
