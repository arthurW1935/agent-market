# CONTRACTS.md — Agent Market frozen interface spec (v1)

**Law of the hackathon: nobody changes this file after 0:15 without both people agreeing out loud.**
Everything here is exact wire format. If code and this doc disagree, the doc wins.

---

## 0. Topology & ports

- Platform (the Layer): `http://localhost:8000`
- Demo agent "sloppy" (seeded-bad): `http://localhost:8001`
- Demo agent "diligent" (good): `http://localhost:8002`
- All agents are external HTTP services. There are NO in-process agents. Registry starts EMPTY at boot.
- All bodies are JSON. All ids are strings. Money is integer coins. Timestamps are ISO-8601 strings.

---

## 1. Enums

```
TaskStatus: CREATED | NO_SUPPLY | RUBRIC_DISCUSSION | CONFIRMED_UNFUNDED |
            FUNDED | ASSIGNED | EXECUTING | VERIFYING | SETTLED | FAILED_UNFULFILLED

EscrowState: locked | released | refunded

EventType: task_posted | no_supply | rubric_proposed | rubric_revised |
           rubric_confirmed | deposit | escrow_locked | candidates_found |
           assigned | dispatched | deliverable_submitted | verdict |
           rerouted | settled | refunded | agent_registered
```

---

## 2. Shared objects

```jsonc
// AgentCard
{ "id": "agt_xxx", "name": "sloppy-writer", "skills": ["research","writing"],
  "price": 40, "endpoint": "http://localhost:8001",
  "rep_score": 3.0, "jobs": 0, "passes": 0, "fails": 0 }

// RubricItem
{ "criterion": "Word count 180-220",
  "checkable_test": "Count words of the deliverable body; pass iff 180 <= n <= 220" }

// Verdict
{ "task_id": "tsk_xxx", "agent_id": "agt_xxx",
  "criteria": [ { "name": "Word count 180-220", "passed": false,
                  "evidence": "\"...counted 341 words...\"", "note": "over limit" } ],
  "overall": false,
  "fix_list": ["Cut to 220 words", "Add 2 citations"] }

// Event (what SSE streams and GET /tasks/{id} returns in "events")
{ "task_id": "tsk_xxx", "type": "verdict", "payload": { /* type-specific */ },
  "ts": "2026-08-08T11:02:33Z" }
```

Event payloads (minimum fields the UI renders):
- `task_posted`: `{spec, bounty}`
- `rubric_proposed` / `rubric_revised`: `{rubric: [RubricItem]}`
- `rubric_confirmed`: `{amount_due}`
- `deposit`: `{owner, amount, balance}`
- `escrow_locked`: `{amount}`
- `candidates_found`: `{count, agent_ids}`
- `assigned` / `dispatched`: `{agent_id, agent_name}`
- `deliverable_submitted`: `{agent_id}`
- `verdict`: full Verdict object
- `rerouted`: `{from_agent, to_agent, attempt}`
- `settled`: `{agent_id, gross, take, net}`
- `refunded`: `{amount}`
- `agent_registered`: full AgentCard

---

## 3. Agent-facing contract (Person 2 implements agents against this)

### 3.1 Register (agent → platform)
```
POST http://localhost:8000/agents
{ "name": "sloppy-writer", "skills": ["research","writing"],
  "price": 40, "endpoint": "http://localhost:8001" }

201 → full AgentCard (platform assigns id, rep_score starts 3.0)
```

### 3.2 Work dispatch (platform → agent)
```
POST {agent.endpoint}/work
{ "task_id": "tsk_xxx",
  "spec": "Research and write a 200-word product brief on X...",
  "rubric": [ RubricItem, ... ],
  "callback_url": "http://localhost:8000/tasks/tsk_xxx/deliverable",
  "agent_token": "opaque-string" }

202 → {"accepted": true}   // agent works async, then calls callback
```
- Platform timeout: if no deliverable callback within **30s** of dispatch → attempt counts as FAIL, reroute.

### 3.3 Deliverable callback (agent → platform)
```
POST http://localhost:8000/tasks/{task_id}/deliverable
{ "agent_id": "agt_xxx", "agent_token": "opaque-string",
  "content": "…the full deliverable text/markdown…" }

202 → {"received": true}   // verification starts; agent's job is done
```
- `agent_token` is whatever the platform sent at dispatch, echoed back (prevents cross-task submissions; NOT real auth).
- Late/duplicate/unknown-token submissions → 409, ignored.

---

## 4. Buyer-facing contract (MCP tools / REST)

```
POST /wallet/deposit          {"owner":"buyer","amount":200}
  → 200 {"owner":"buyer","balance":200}

POST /tasks                   {"spec":"...","bounty":100,
                               "auto_confirm":false,"auto_fund":false}
  → 201 Task (status RUBRIC_DISCUSSION, rubric filled)
  → or 200 {"status":"NO_SUPPLY","nearest_capabilities":["extraction"]}

POST /tasks/{id}/rubric/message   {"message":"criterion 2 too strict"}
  → 200 {"rubric":[...], "changes":"relaxed word range to 170-230"}
  → 409 if status != RUBRIC_DISCUSSION

POST /tasks/{id}/rubric/confirm   {}    // optional body {"rubric":[...]} = confirm-with-edits
  → 402 {"amount_due":100,"fund_via":"POST /tasks/{id}/fund"}   // rubric now FROZEN

POST /tasks/{id}/fund             {}
  → 200 Task (status FUNDED; pipeline auto-starts)
  → 400 {"error":"insufficient_balance","balance":50,"needed":100}

GET  /tasks/{id}
  → 200 { ...Task, "events":[Event,...],
          "deliverable": "…" }        // deliverable field ONLY when SETTLED

GET  /tasks/{id}/deliverable
  → 423 until SETTLED, then 200 {"content":"…"}

GET  /agents
  → 200 [AgentCard, ...]  // sorted rep_score desc

GET  /events/{task_id}     // SSE, each message = one Event JSON
```

Task object shape (returned everywhere):
```jsonc
{ "id":"tsk_xxx", "spec":"...", "bounty":100, "status":"FUNDED",
  "rubric":[RubricItem], "assigned_agent":"agt_xxx"|null,
  "attempts":["agt_a"], "created_at":"..." }
```

---

## 5. Business rules (both sides must honor)

- Max **3** attempts per task, then FAILED_UNFULFILLED + refund.
- Take-rate: **5%** at release (settled payload shows gross/take/net).
- Rubric immutable after confirm → rubric routes return 409.
- Supply check at create: no registered agent has ALL required skills → NO_SUPPLY (skills required are inferred from spec by the platform; agents match on exact skill strings — use only: "research", "writing", "extraction").
- Buyer never receives deliverable content unless SETTLED (verdicts yes, work no).
- Verifier input = spec + rubric + deliverable ONLY.
- New agents register with rep_score 3.0. Verdict PASS: +0.3 (cap 5.0). FAIL: −0.4 (floor 1.0). Ranking = rep_score / price.

---

## 6. Demo choreography (both build toward exactly this)

1. Boot platform (empty registry) → UI shows empty marketplace
2. `sloppy-writer` (:8001) and `diligent-writer` (:8002) self-register on startup → cards appear
3. Buyer script: deposit 200 → create task (brief on X, bounty 100) → confirm → 402 → fund
4. Discovery ranks: sloppy (rep 3.0/price 40 = 0.075) beats diligent (3.0/55 = 0.055) → sloppy assigned
5. Sloppy submits bad work → verdict FAIL (evidence-quoted) → rep 2.6 → reroute → diligent
6. Diligent submits → PASS → settle: +95 diligent, +5 platform, deliverable unlocks, rep 3.3
7. (Encore if time) third agent registers live mid-demo

Seeding note: sloppy's price is set LOWER so ranking legitimately picks it first. Do not hack the ranking.
