"""End-to-end smoke test (DESIGN.md §9).

Boots two stub agents in-process (sloppy on :8001, diligent on :8002),
registers them, runs the full buyer flow against a platform on :8000, and
asserts the exact event sequence, terminal status, money invariant, and
reputation invariant of the fail -> reroute -> pass arc.

Run modes:
    MOCK_LLM=1 uvicorn main:app --port 8000     # platform, mock mode
    python smoke.py                              # asserts mock verdicts
    python smoke.py --live                       # platform runs real LLM calls
                                                 # (start it WITHOUT MOCK_LLM)

In mock mode the verifier fails any deliverable containing "MOCK_FAIL";
the sloppy stub plants that marker. In live mode the sloppy stub submits
genuinely rubric-violating work and Opus fails it on the merits.
"""

from __future__ import annotations

import argparse
import asyncio
import sys

import httpx
import uvicorn
from fastapi import FastAPI

PLATFORM = "http://localhost:8000"

SLOPPY_CONTENT = (
    "MOCK_FAIL Heres some stuff about the thing. "
    "It is very good and everyone should definitely buy it because it is the best product "
    "that has ever existed in the entire history of products and nobody could ever possibly "
    "disagree with that statement no matter what happens ever. Trust me."
)

DILIGENT_CONTENT = (
    "Product brief: X is a workflow tool that verifiably cuts review time for small teams.\n"
    "X targets engineering teams of five to fifty people. It automates review assignment. "
    "It tracks turnaround metrics. Independent trials report a thirty percent cycle reduction "
    "(https://example.com/study-2026). Pricing starts at ten dollars per seat "
    "(https://example.com/pricing). "
    + "It integrates with common code hosts. Setup takes under an hour. "
    "Teams keep their existing branching model. Reviewers get balanced queues. "
    "Managers get weekly reports. Data stays in the customer cloud. "
    "Support responds within one business day. A free tier covers three users. "
    "Annual billing saves twenty percent. Migration tooling imports history. "
    "The roadmap adds audit logs next quarter. Early adopters praise the onboarding flow. "
    "Documentation covers every endpoint. Uptime exceeded targets last year. "
    "Security reviews run quarterly. The team ships weekly. Customers renew at high rates. "
    "X suits teams that want faster reviews without process change. "
    "It earns its seat cost within one sprint. Choose X when review latency hurts delivery. "
    "The trial needs no credit card. Install it today and measure the difference this week."
)


def make_stub(name: str, content: str, delay: float) -> FastAPI:
    """Minimal external agent: 202 on /work, then POSTs the callback."""
    stub = FastAPI(title=f"stub-{name}")

    @stub.post("/work", status_code=202)
    async def work(body: dict):
        async def submit():
            await asyncio.sleep(delay)
            async with httpx.AsyncClient(timeout=10) as client:
                await client.post(body["callback_url"], json={
                    "agent_id": stub.state.agent_id,
                    "agent_token": body["agent_token"],
                    "content": content,
                })
        asyncio.get_running_loop().create_task(submit())
        return {"accepted": True}

    return stub


async def run_stub(app: FastAPI, port: int) -> uvicorn.Server:
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error"))
    asyncio.get_running_loop().create_task(server.serve())
    while not server.started:
        await asyncio.sleep(0.05)
    return server


def check(label: str, condition: bool, detail: str = "") -> None:
    print(f"{'PASS' if condition else 'FAIL'}  {label}  {detail}")
    if not condition:
        sys.exit(1)


async def main(live: bool) -> None:
    sloppy_app = make_stub("sloppy", SLOPPY_CONTENT, delay=1.0)
    diligent_app = make_stub("diligent", DILIGENT_CONTENT, delay=1.0)
    await run_stub(sloppy_app, 8001)
    await run_stub(diligent_app, 8002)

    async with httpx.AsyncClient(base_url=PLATFORM, timeout=120) as client:
        # Demo choreography step 2: agents self-register.
        r = await client.post("/agents", json={
            "name": "sloppy-writer", "skills": ["research", "writing"],
            "price": 40, "endpoint": "http://localhost:8001"})
        sloppy = r.json()
        sloppy_app.state.agent_id = sloppy["id"]
        r = await client.post("/agents", json={
            "name": "diligent-writer", "skills": ["research", "writing"],
            "price": 55, "endpoint": "http://localhost:8002"})
        diligent = r.json()
        diligent_app.state.agent_id = diligent["id"]
        check("two agents registered", sloppy["rep_score"] == 3.0 and diligent["rep_score"] == 3.0)

        # Step 3: buyer flow.
        r = await client.post("/wallet/deposit", json={"owner": "buyer", "amount": 200})
        check("deposit 200", r.json()["balance"] == 200)

        r = await client.post("/tasks", json={
            "spec": "Research and write a 200-word product brief on X, citing at least 2 sources.",
            "bounty": 100})
        check("create -> 201 RUBRIC_DISCUSSION", r.status_code == 201
              and r.json()["status"] == "RUBRIC_DISCUSSION", str(r.status_code))
        task_id = r.json()["id"]

        r = await client.post(f"/tasks/{task_id}/rubric/confirm", json={})
        check("confirm -> 402 amount_due 100", r.status_code == 402
              and r.json()["amount_due"] == 100, r.text)

        r = await client.post(f"/tasks/{task_id}/fund")
        check("fund -> FUNDED", r.status_code == 200 and r.json()["status"] == "FUNDED", r.text)

        # Pipeline runs autonomously; poll to terminal state.
        body = None
        for _ in range(180):
            await asyncio.sleep(1)
            body = (await client.get(f"/tasks/{task_id}")).json()
            if body["status"] in ("SETTLED", "FAILED_UNFULFILLED"):
                break
        check("terminal status SETTLED", body["status"] == "SETTLED", body["status"])

        # Event sequence: the fail -> reroute -> pass arc, exactly.
        types = [e["type"] for e in body["events"]]
        expected = [
            "task_posted", "rubric_proposed", "rubric_confirmed", "escrow_locked",
            "candidates_found", "assigned", "dispatched", "deliverable_submitted",
            "verdict", "rerouted", "assigned", "dispatched", "deliverable_submitted",
            "verdict", "settled",
        ]
        check("event sequence (fail -> reroute -> pass)", types == expected, str(types))

        verdicts = [e["payload"] for e in body["events"] if e["type"] == "verdict"]
        check("verdict 1 = FAIL by sloppy",
              verdicts[0]["overall"] is False and verdicts[0]["agent_id"] == sloppy["id"])
        check("verdict evidence present on every criterion",
              all(c["evidence"] for v in verdicts for c in v["criteria"]))
        check("verdict 2 = PASS by diligent",
              verdicts[1]["overall"] is True and verdicts[1]["agent_id"] == diligent["id"])

        reroute = next(e["payload"] for e in body["events"] if e["type"] == "rerouted")
        check("reroute sloppy -> diligent, reason verdict_failed",
              reroute["from_agent"] == sloppy["id"] and reroute["to_agent"] == diligent["id"]
              and reroute["reason"] == "verdict_failed", str(reroute))

        # Ranking check: sloppy (3.0/40) legitimately beat diligent (3.0/55).
        check("ranking picked sloppy first (rep/price)", body["attempts"][0] == sloppy["id"])

        # Money invariant.
        wallets = (await client.get("/wallets")).json()
        check("buyer 100 / diligent +95 / platform +5 / sloppy 0",
              wallets.get("buyer") == 100 and wallets.get(diligent["id"]) == 95
              and wallets.get("platform") == 5 and wallets.get(sloppy["id"], 0) == 0,
              str(wallets))

        # Reputation invariant.
        agents = {a["id"]: a for a in (await client.get("/agents")).json()}
        check("sloppy rep 2.6, fails 1", agents[sloppy["id"]]["rep_score"] == 2.6
              and agents[sloppy["id"]]["fails"] == 1)
        check("diligent rep 3.3, passes 1", agents[diligent["id"]]["rep_score"] == 3.3
              and agents[diligent["id"]]["passes"] == 1)

        # Deliverable unlocked to buyer only now.
        r = await client.get(f"/tasks/{task_id}/deliverable")
        check("deliverable unlocked after settle", r.status_code == 200
              and r.json()["content"].startswith("Product brief"), str(r.status_code))

    print("\nSMOKE PASSED — full fail -> reroute -> pass arc verified"
          + (" (live LLM)" if live else " (mock)"))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--live", action="store_true",
                        help="platform is running real LLM calls (no MOCK_LLM)")
    args = parser.parse_args()
    asyncio.run(main(args.live))
