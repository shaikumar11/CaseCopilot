
import json
import os
import re
import sqlite3

from fastapi import Depends, FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from app.auth import current_user, router


DB = os.environ.get("DB_PATH", "cases.db")

app = FastAPI(title="CaseCopilot")
app.include_router(router)


KB = [
    (
        1,
        "Reset your password",
        "reset password login locked sign forgot",
        "Use the Forgot password link on the sign in page. "
        "The reset email arrives within 5 minutes and the link works for 1 hour."
    ),
    (
        2,
        "Update billing details",
        "billing invoice card payment update charge",
        "Open Settings, then Billing, and edit the card or invoice details. "
        "Changes apply from the next billing date."
    ),
    (
        3,
        "Export your data",
        "export download csv report data backup",
        "Go to Reports, choose a date range and click Export. "
        "Large exports are emailed as a CSV link."
    ),
    (
        4,
        "Connect to Slack",
        "slack integration connect notifications channel",
        "Open Integrations, pick Slack, approve access and choose a channel "
        "for notifications."
    ),
    (
        5,
        "Add or remove users",
        "user seat invite remove team member admin",
        "Admins can invite or remove users under Settings, then Team. "
        "Removed users lose access immediately."
    )
]


HIGH = (
    "outage",
    "down for",
    "breach",
    "urgent",
    "charged twice",
    "cannot access"
)

SENSITIVE = (
    "refund",
    "lawyer",
    "legal",
    "chargeback",
    "gdpr"
)


def db():
    c = sqlite3.connect(DB)
    c.row_factory = sqlite3.Row

    c.execute(
        "create table if not exists cases("
        "id integer primary key autoincrement, "
        "customer text, "
        "subject text, "
        "body text, "
        "status text default 'new', "
        "priority text default 'normal', "
        "reply text default '', "
        "trace text default '[]')"
    )

    return c


class NewCase(BaseModel):
    customer: str = Field(min_length=1)
    subject: str = Field(min_length=1)
    body: str = Field(min_length=1)


# ---------------- AGENT TOOLS ----------------

def mask_pii(t):
    return re.sub(
        r"\b\d{10,}\b",
        "[number]",
        re.sub(
            r"[\w.+-]+@[\w-]+\.[\w.]+",
            "[email]",
            t
        )
    )


def words(t):
    return set(re.findall(r"[a-z]+", t.lower()))


def search_kb(query):
    return max(
        (
            (
                len(words(query) & words(a[1] + " " + a[2])),
                a
            )
            for a in KB
        ),
        key=lambda x: x[0]
    )


def run_agent(case):
    text = case["subject"] + " " + case["body"]
    low = text.lower()

    prio = "high" if any(w in low for w in HIGH) else "normal"

    trace = [
        {
            "tool": "set_priority",
            "input": case["subject"],
            "output": prio
        }
    ]

    if any(w in low for w in SENSITIVE):
        trace.append({
            "tool": "escalate",
            "input": "policy guardrail",
            "output": "sensitive topic sent to a human"
        })

        return prio, "escalated", "", trace

    score, art = search_kb(text)

    trace.append({
        "tool": "search_kb",
        "input": case["subject"],
        "output": f"{art[1]} (match {score})"
    })

    if score < 2:
        trace.append({
            "tool": "escalate",
            "input": "low confidence",
            "output": "no confident article, sent to a human"
        })

        return prio, "escalated", "", trace

    reply = (
        f"Hi {case['customer']}, thanks for contacting us. "
        f"{art[3]} Reply here if you still need help."
    )

    trace.append({
        "tool": "draft_reply",
        "input": art[1],
        "output": "draft ready for manager review"
    })

    return prio, "drafted", reply, trace


def load(row):
    d = dict(row)
    d["trace"] = json.loads(d["trace"])
    return d


def fetch(c, cid):
    row = c.execute(
        "select * from cases where id=?",
        (cid,)
    ).fetchone()

    if not row:
        raise HTTPException(404, "case not found")

    return load(row)


def process(c, cid):
    prio, status, reply, trace = run_agent(fetch(c, cid))

    c.execute(
        "update cases set priority=?, status=?, reply=?, trace=? where id=?",
        (prio, status, reply, json.dumps(trace), cid)
    )


# ---------------- API ENDPOINTS ----------------

@app.get("/api/cases")
def cases(user=Depends(current_user)):
    with db() as c:
        return [
            load(r)
            for r in c.execute(
                "select * from cases order by id desc"
            )
        ]


@app.post("/api/cases", status_code=201)
def create(n: NewCase, user=Depends(current_user)):
    with db() as c:
        cur = c.execute(
            "insert into cases(customer,subject,body) values(?,?,?)",
            (
                n.customer,
                mask_pii(n.subject),
                mask_pii(n.body)
            )
        )

        return fetch(c, cur.lastrowid)


@app.post("/api/cases/{cid}/run")
def run(cid: int, user=Depends(current_user)):
    with db() as c:
        if fetch(c, cid)["status"] != "new":
            raise HTTPException(
                409,
                "the agent already ran on this case"
            )

        process(c, cid)

        return fetch(c, cid)


@app.post("/api/cases/{cid}/approve")
def approve(cid: int, user=Depends(current_user)):
    if user["role"] != "manager":
        raise HTTPException(
            403,
            "only managers can approve replies"
        )

    with db() as c:
        if fetch(c, cid)["status"] != "drafted":
            raise HTTPException(
                409,
                "there is no draft to approve"
            )

        c.execute(
            "update cases set status='sent' where id=?",
            (cid,)
        )

        return fetch(c, cid)


@app.get("/api/stats")
def stats(user=Depends(current_user)):
    with db() as c:
        n = {
            s: c.execute(
                "select count(*) from cases where status=?",
                (s,)
            ).fetchone()[0]
            for s in ("new", "drafted", "escalated", "sent")
        }

    handled = n["drafted"] + n["sent"] + n["escalated"]

    return {
        **n,
        "total": sum(n.values()),
        "auto_draft_rate": (
            round(100 * (n["drafted"] + n["sent"]) / handled)
            if handled else 0
        )
    }


# ---------------- DEMO DATA ----------------

def seed_demo():
    items = [
        (
            "Neha",
            "Cannot reset password",
            "I am locked out and need to reset my password. "
            "My email is neha@acme.com"
        ),
        (
            "Karan",
            "Update card for invoice",
            "Please help me update the billing card on my invoice"
        ),
        (
            "Divya",
            "Urgent: Slack integration down",
            "Our slack notifications channel integration is down for hours"
        ),
        (
            "Arjun",
            "Refund request",
            "I want a refund for last month"
        ),
        (
            "Sana",
            "Export report as CSV",
            "How do I export my data as a CSV report?"
        ),
        (
            "Ravi",
            "Question about roadmap",
            "When will the mobile app launch?"
        )
    ]

    with db() as c:
        if c.execute(
            "select count(*) from cases"
        ).fetchone()[0] == 0:

            ids = [
                c.execute(
                    "insert into cases(customer,subject,body) values(?,?,?)",
                    (
                        who,
                        mask_pii(subj),
                        mask_pii(body)
                    )
                ).lastrowid
                for who, subj, body in items
            ]

            for cid in ids[:4]:
                process(c, cid)

            c.execute(
                "update cases set status='sent' where id=?",
                (ids[1],)
            )


if os.environ.get("SEED_DEMO") == "1":
    seed_demo()


# ---------------- HOMEPAGE ----------------

@app.get("/")
def index():
    return FileResponse(
        os.path.join(
            os.path.dirname(__file__),
            "static",
            "index.html"
        )
    )
