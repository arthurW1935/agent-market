"""mock.py — offline demo insurance. Impersonates the platform (:8000) for the UI.

Replays the full CONTRACTS §6 choreography (register ×2 → task → rubric → fund →
sloppy FAIL → reroute → diligent PASS → settle) with no LLMs, no agents, no wifi.

    python mock.py            # serve canned /agents, /tasks/{id}, SSE /events/{id} on :8000
    python mock.py --dump     # print the raw JSON event list (UI can load it statically)
"""
import asyncio
import json
import sys

SLOPPY = {"id": "agt_sloppy", "name": "sloppy-writer", "skills": ["research", "writing"],
          "price": 40, "endpoint": "http://localhost:8001", "rep_score": 3.0,
          "jobs": 0, "passes": 0, "fails": 0}
DILIGENT = {"id": "agt_diligent", "name": "diligent-writer", "skills": ["research", "writing"],
            "price": 55, "endpoint": "http://localhost:8002", "rep_score": 3.0,
            "jobs": 0, "passes": 0, "fails": 0}
RUBRIC = [
    {"criterion": "Word count 180-220", "checkable_test": "Count words of the body; pass iff 180 <= n <= 220"},
    {"criterion": "Cites at least 2 real sources", "checkable_test": "Body names >= 2 identifiable publications/organizations"},
    {"criterion": "Targets outdoor enthusiasts", "checkable_test": "Body explicitly addresses the outdoor/camping/hiking audience"},
    {"criterion": "No filler cliches", "checkable_test": "Body contains no stock phrases like 'in today's fast-paced world'"},
]
DELIVERABLE = ("SolarPeak 20W Trail Charger — product brief.\n\nBuilt for hikers and "
               "campers who are days from an outlet, the SolarPeak 20W folds to notebook "
               "size, weighs 480 g, and clips to any pack. Its monocrystalline panels hit "
               "23% conversion efficiency, restoring a phone to 50% in about 90 minutes of "
               "direct sun. An integrated 10,000 mAh buffer battery banks daytime surplus "
               "so devices charge after dark — the failure point of panel-only rivals. "
               "IP65 sealing shrugs off rain and trail dust; dual USB-C ports fast-charge "
               "a phone and headlamp at once.\n\nThe market is moving with it: Grand View "
               "Research values portable solar chargers at $1.1B by 2027, and an Outdoor "
               "Industry Association survey found 68% of backpackers now carry two or more "
               "rechargeable devices. Positioned at $89 — between bare panels and heavy "
               "power stations — SolarPeak targets weekend warriors and thru-hikers alike. "
               "Early field reviews on GearJunkie praise its weight-to-output ratio as "
               "best in class. SolarPeak: sunlight in, adventure on. (Word count: 198)")

T = "tsk_demo1"
# Non-task events (agent_registered, deposit) carry task_id null and appear
# only in the global /events stream — DESIGN.md §3.
EVENTS = [
    {"task_id": None, "type": "agent_registered", "payload": SLOPPY, "ts": "2026-08-08T11:00:01Z"},
    {"task_id": None, "type": "agent_registered", "payload": DILIGENT, "ts": "2026-08-08T11:00:03Z"},
    {"task_id": None, "type": "deposit", "payload": {"owner": "buyer", "amount": 200, "balance": 200}, "ts": "2026-08-08T11:00:10Z"},
    {"task_id": T, "type": "task_posted", "payload": {"spec": "Research and write a 200-word product brief on a solar-powered phone charger for outdoor enthusiasts. Cite at least 2 real sources.", "bounty": 100}, "ts": "2026-08-08T11:00:12Z"},
    {"task_id": T, "type": "rubric_proposed", "payload": {"rubric": RUBRIC}, "ts": "2026-08-08T11:00:15Z"},
    {"task_id": T, "type": "rubric_confirmed", "payload": {"amount_due": 100}, "ts": "2026-08-08T11:00:20Z"},
    {"task_id": T, "type": "escrow_locked", "payload": {"amount": 100}, "ts": "2026-08-08T11:00:22Z"},
    {"task_id": T, "type": "candidates_found", "payload": {"count": 2, "agent_ids": ["agt_sloppy", "agt_diligent"]}, "ts": "2026-08-08T11:00:23Z"},
    {"task_id": T, "type": "assigned", "payload": {"agent_id": "agt_sloppy", "agent_name": "sloppy-writer"}, "ts": "2026-08-08T11:00:24Z"},
    {"task_id": T, "type": "dispatched", "payload": {"agent_id": "agt_sloppy", "agent_name": "sloppy-writer"}, "ts": "2026-08-08T11:00:25Z"},
    {"task_id": T, "type": "deliverable_submitted", "payload": {"agent_id": "agt_sloppy"}, "ts": "2026-08-08T11:00:33Z"},
    {"task_id": T, "type": "verdict", "payload": {
        "task_id": T, "agent_id": "agt_sloppy",
        "criteria": [
            {"name": "Word count 180-220", "passed": False, "evidence": "\"...counted 379 words...\"", "note": "nearly double the limit"},
            {"name": "Cites at least 2 real sources", "passed": False, "evidence": "\"no publication or organization is named anywhere\"", "note": "zero citations"},
            {"name": "Targets outdoor enthusiasts", "passed": True, "evidence": "\"...perfect for your next camping trip...\"", "note": ""},
            {"name": "No filler cliches", "passed": False, "evidence": "\"in today's fast-paced world, game-changing synergy...\"", "note": "stock filler throughout"},
        ],
        "overall": False,
        "fix_list": ["Cut to 220 words", "Add 2 named sources", "Remove filler phrases"]}, "ts": "2026-08-08T11:00:41Z"},
    {"task_id": T, "type": "rerouted", "payload": {"from_agent": "agt_sloppy", "to_agent": "agt_diligent", "attempt": 2, "reason": "verdict_failed"}, "ts": "2026-08-08T11:00:43Z"},
    {"task_id": T, "type": "assigned", "payload": {"agent_id": "agt_diligent", "agent_name": "diligent-writer"}, "ts": "2026-08-08T11:00:44Z"},
    {"task_id": T, "type": "dispatched", "payload": {"agent_id": "agt_diligent", "agent_name": "diligent-writer"}, "ts": "2026-08-08T11:00:45Z"},
    {"task_id": T, "type": "deliverable_submitted", "payload": {"agent_id": "agt_diligent"}, "ts": "2026-08-08T11:00:54Z"},
    {"task_id": T, "type": "verdict", "payload": {
        "task_id": T, "agent_id": "agt_diligent",
        "criteria": [
            {"name": "Word count 180-220", "passed": True, "evidence": "\"...counted 198 words...\"", "note": ""},
            {"name": "Cites at least 2 real sources", "passed": True, "evidence": "\"Grand View Research... Outdoor Industry Association...\"", "note": "3 sources named"},
            {"name": "Targets outdoor enthusiasts", "passed": True, "evidence": "\"Built for hikers and campers who are days from an outlet\"", "note": ""},
            {"name": "No filler cliches", "passed": True, "evidence": "\"no stock phrases found\"", "note": ""},
        ],
        "overall": True, "fix_list": []}, "ts": "2026-08-08T11:01:02Z"},
    {"task_id": T, "type": "settled", "payload": {"agent_id": "agt_diligent", "gross": 100, "take": 5, "net": 95}, "ts": "2026-08-08T11:01:03Z"},
]


def serve():
    import uvicorn
    from fastapi import FastAPI
    from fastapi.responses import StreamingResponse

    app = FastAPI(title="agent-market-mock")

    @app.get("/agents")
    async def agents():
        return [dict(SLOPPY, jobs=1, fails=1, rep_score=2.6),
                dict(DILIGENT, jobs=1, passes=1, rep_score=3.3)]

    @app.get("/tasks/{task_id}")
    async def task(task_id: str):
        return {"id": task_id, "spec": EVENTS[3]["payload"]["spec"], "bounty": 100,
                "status": "SETTLED", "rubric": RUBRIC, "assigned_agent": "agt_diligent",
                "attempts": ["agt_sloppy", "agt_diligent"], "created_at": EVENTS[3]["ts"],
                "events": [e for e in EVENTS if e["task_id"] == task_id],
                "deliverable": DELIVERABLE}

    @app.get("/tasks/{task_id}/deliverable")
    async def deliverable(task_id: str):
        return {"content": DELIVERABLE}

    def sse_replay(events):
        async def stream():
            for ev in events:
                yield f"data: {json.dumps(ev)}\n\n"
                await asyncio.sleep(1.5)
        return StreamingResponse(stream(), media_type="text/event-stream")

    @app.get("/events")
    async def events_global():  # the UI's single subscription (DESIGN.md §3, §8)
        return sse_replay(EVENTS)

    @app.get("/events/{task_id}")
    async def events_task(task_id: str):  # per-task stream per CONTRACTS §4
        return sse_replay([e for e in EVENTS if e["task_id"] == task_id])

    print("mock platform on :8000 — SSE replay at /events (global) and /events/tsk_demo1", flush=True)
    uvicorn.run(app, host="127.0.0.1", port=8000)


if __name__ == "__main__":
    if "--dump" in sys.argv:
        print(json.dumps(EVENTS, indent=2))
    else:
        serve()
