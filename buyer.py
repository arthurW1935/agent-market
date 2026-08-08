"""buyer.py — plays "the buyer's Claude" against the Agent Market platform (:8000).

Drives the full CONTRACTS §4 flow: deposit → create task → (optional rubric round)
→ confirm (expect 402) → fund → live-poll events → fetch the deliverable.

    python buyer.py --auto              # no pauses, for testing
    python buyer.py --demo              # pause on Enter between beats, for stage
    python buyer.py --demo --message "criterion 2 is too strict"
"""
import argparse
import sys
import time

import httpx

PLATFORM = "http://localhost:8000"
DEMO_SPEC = ("Research and write a 200-word product brief on a solar-powered phone "
             "charger for outdoor enthusiasts. Cite at least 2 real sources.")
TERMINAL = {"SETTLED", "FAILED_UNFULFILLED", "NO_SUPPLY"}


def beat(msg: str, demo: bool):
    print(f"\n▶ {msg}")
    if demo:
        input("  [Enter] ")


def print_event(ev: dict):
    t, p = ev["type"], ev.get("payload", {})
    if t == "verdict":
        print(f"  ⚖ verdict: {'PASS ✅' if p['overall'] else 'FAIL ❌'}")
        for c in p["criteria"]:
            print(f"      {'✓' if c['passed'] else '✗'} {c['name']} — {c.get('evidence', '')}")
        if not p["overall"]:
            print(f"      fix list: {p.get('fix_list')}")
    elif t == "settled":
        print(f"  💸 settled: gross {p['gross']} → take {p['take']} → net {p['net']} to {p['agent_id']}")
    else:
        print(f"  · {t}: {p}")


def main():
    ap = argparse.ArgumentParser()
    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument("--auto", action="store_true", help="no pauses (testing)")
    mode.add_argument("--demo", action="store_true", help="pause on Enter between beats (stage)")
    ap.add_argument("--spec", default=DEMO_SPEC)
    ap.add_argument("--bounty", type=int, default=100)
    ap.add_argument("--message", help="one rubric discussion message before confirming")
    args = ap.parse_args()
    demo = args.demo
    http = httpx.Client(base_url=PLATFORM, timeout=30)

    beat("deposit_funds(200)", demo)
    r = http.post("/wallet/deposit", json={"owner": "buyer", "amount": 200})
    print(f"  balance: {r.json()['balance']}")

    beat(f"create_task(bounty={args.bounty})", demo)
    r = http.post("/tasks", json={"spec": args.spec, "bounty": args.bounty,
                                  "auto_confirm": False, "auto_fund": False})
    body = r.json()
    if body.get("status") == "NO_SUPPLY":
        print(f"  ✋ NO_SUPPLY — nearest capabilities: {body.get('nearest_capabilities')}")
        sys.exit(1)
    task_id = body["id"]
    print(f"  task {task_id} → {body['status']}\n  proposed rubric:")
    for item in body["rubric"]:
        print(f"    - {item['criterion']}")

    if args.message:
        beat(f'discuss_rubric("{args.message}")', demo)
        r = http.post(f"/tasks/{task_id}/rubric/message", json={"message": args.message})
        print(f"  changes: {r.json().get('changes')}\n  revised rubric:")
        for item in r.json()["rubric"]:
            print(f"    - {item['criterion']}")

    beat("confirm_rubric() — expect 402 Payment Required", demo)
    r = http.post(f"/tasks/{task_id}/rubric/confirm", json={})
    assert r.status_code == 402, f"expected 402, got {r.status_code}: {r.text}"
    print(f"  402 → amount_due: {r.json()['amount_due']} (rubric now FROZEN)")

    beat("fund_escrow()", demo)
    r = http.post(f"/tasks/{task_id}/fund", json={})
    r.raise_for_status()
    print(f"  status: {r.json()['status']} — escrow locked, pipeline running")

    print("\n▶ watching events…")
    seen = 0
    while True:
        task = http.get(f"/tasks/{task_id}").json()
        for ev in task.get("events", [])[seen:]:
            print_event(ev)
        seen = len(task.get("events", []))
        if task["status"] in TERMINAL:
            print(f"\n▶ terminal state: {task['status']}")
            break
        time.sleep(1)

    if task["status"] == "SETTLED":
        beat("get_deliverable()", demo)
        r = http.get(f"/tasks/{task_id}/deliverable")
        print(f"\n===== DELIVERABLE =====\n{r.json()['content']}\n=======================")


if __name__ == "__main__":
    main()
