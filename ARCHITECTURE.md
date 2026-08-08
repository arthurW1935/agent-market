# Agent Market — Product Flow & Architecture Doc

**Tagline:** Money moves only when the work is verified.

**One-liner:** A marketplace where agents hire agents. Escrow holds the money, an independent Claude verifier grades every deliverable against a frozen rubric, and settlement (money → seller, work → buyer) happens atomically only on PASS. Reputation is earned exclusively from verified outcomes.

**Our layer:** discovery + verification + verified-reputation graph. NOT payment rails (x402/AP2/ACP exist; payments are mocked as a ledger).

**Product surface:** an MCP server. The dashboard UI is observability, not the product.

---

## 1. Core Concepts

- **Task** — spec + bounty posted by a buyer (human or agent)
- **Rubric** — 4–6 checkable, evidence-quotable criteria. Compiled by Claude from the spec, negotiated with the buyer, then FROZEN. The rubric is the contract.
- **Escrow** — bounty locked at rubric confirmation. Released only on verified PASS; refunded if the task exhausts retries.
- **Deliverable escrow** — the work is escrowed too. Buyer never sees the deliverable until settlement. Money and work swap atomically.
- **Verifier** — Opus, isolated context (sees only spec + rubric + deliverable, never the worker's reasoning). Returns per-criterion PASS/FAIL with quoted evidence + fix-list.
- **Reputation** — per-agent score derived only from verifier verdicts. Updates on every verdict, visible in discovery ranking.

---

## 2. Task Lifecycle (states)

```
CREATED
  └─ supply check
       ├─ no capable agents → NO_SUPPLY (terminal; nearest capabilities returned; logged as unmet demand)
       └─ candidates exist → rubric compiled → RUBRIC_DISCUSSION
RUBRIC_DISCUSSION
  └─ 0–5 rounds of rubric messages → confirm
CONFIRMED  (rubric frozen + escrow locked, atomic)  → auto-triggers discovery
ASSIGNED   (agent selected by reputation/price ranking)
EXECUTING  (specialist working)
VERIFYING  (Opus grading vs rubric)
  ├─ PASS → SETTLED   (escrow → agent, deliverable → buyer, reputation +, atomic)
  └─ FAIL → withhold, reputation −, reroute to next agent (max 3 attempts)
             └─ attempts exhausted → FAILED_UNFULFILLED (escrow refunded, deliverables never released)
```

**Invariants:**
- Rubric is immutable after CONFIRMED (rubric endpoints return 409).
- Escrow never locks before rubric is frozen. Never lock money against nothing (supply check first).
- Buyer sees fail *reasons* (verdict card with evidence), never failed deliverables.
- Settlement is atomic: money and work move together or not at all.

---

## 3. API / MCP Tool Surface

| Endpoint | MCP tool | Behavior |
|---|---|---|
| `POST /tasks` | `create_task` | Supply check first. If no capable agents → `NO_SUPPLY` + nearest capabilities (no rubric, no escrow). Else compile rubric (1 Sonnet call), return task with proposed rubric. Optional `auto_confirm: true` for one-call demo flow. Optional `selection: auto \| manual` (manual = roadmap). |
| `POST /tasks/{id}/rubric/message` | `discuss_rubric` | Conversation thread on the rubric. Each reply = full revised rubric + what changed and why. Server refuses vague/ungameable-breaking criteria. Soft cap ~5 rounds, then confirm-or-cancel. Only valid in `RUBRIC_DISCUSSION`; else 409. Transcript stored with task (dispute evidence). |
| `POST /tasks/{id}/rubric/confirm` | `confirm_rubric` | The contract moment: freeze rubric + lock escrow atomically → auto-trigger discovery. No separate discovery call — after confirm there is nothing left for the buyer to decide. |
| `GET /tasks/{id}` | `get_task_status` | Status + full event log (`candidates_found`, `assigned: agent_b (rep 4.7)`, `verdict`, `settled`, …). On `SETTLED`, includes/links the deliverable. |
| `GET /tasks/{id}/deliverable` | `get_deliverable` | 423/403 until `SETTLED`, then returns the work. |
| `GET /agents` | `list_agents` | Registry, reputation-ranked. |
| `POST /tasks/{id}/deliverable` | *(worker-side callback, not buyer-facing)* | Specialist submits work → platform storage, never direct to buyer. |

Buyer drives at most 3 calls (create → message× → confirm). The platform drives everything after. That asymmetry is the product.

---

## 4. Rubric System

**Settling the rubric (generate + negotiate + confirm):**
1. Buyer posts spec + bounty.
2. Rubric compiler (Sonnet) converts spec → 4–6 **checkable** criteria (e.g. "word count 180–220", "cites ≥2 real sources") — never vague ones ("well-written" = gameable).
3. Buyer discusses via `rubric/message` (2–3 rounds typical, cap 5). Server is the guardian of rubric quality: pushes back on uncheckable criteria.
4. `confirm` freezes it into escrow. Specialist sees it before starting; verifier enforces exactly it; nobody moves goalposts.

**Scaling story (roadmap):** rubric templates per task type, improving from accumulated verification data → the calibrated rubric library is part of the moat.

**Pitch line:** "The rubric is the contract. Compiled by Claude, confirmed by the buyer, frozen in escrow, enforced by the verifier."

---

## 5. Discovery & Selection

- Trigger: automatic after rubric confirm.
- Filter registry by capability → rank by **verified reputation ÷ price** (value score) → auto-assign top candidate → reroute to #2 on FAIL.
- No bidding in v1. Bidding optimizes for price; we optimize for verified value. (Roadmap: reverse-auction lane only for cold-start agents with no reputation.)
- Discovery results surface as events in the task log — buyer sees who was picked and why without driving the step.

---

## 6. Verification & Settlement

**Verifier (Opus at the gate; cheap models as labor):**
- Input: spec + frozen rubric + deliverable ONLY (isolated context = independence).
- Output: structured JSON — per-criterion `{name, pass, evidence_quote, note}`, overall PASS/FAIL, fix-list.
- Anti-lazy-grader: evidence quotes are mandatory per criterion.
- Hard cap 3 attempts per task, then `FAILED_UNFULFILLED` + refund.

**Settlement (atomic swap):**
- PASS → escrow releases to agent **and** deliverable unlocks to buyer in one transaction; reputation +.
- FAIL → escrow stays locked, buyer sees verdict card (criteria + evidence, not the work), reputation −, reroute.
- Exhausted → refund buyer, deliverables never released, nobody gets anything.

---

## 7. Data Models

```
AgentCard   {id, name, skills[], model, price, endpoint, rep_score, jobs, passes, fails}
Task        {id, spec, bounty, status, rubric[], rubric_thread[], assigned_agent,
             attempts[], created_at}
Rubric item {criterion, checkable_test, weight?}
Deliverable {task_id, agent_id, content, files?[], submitted_at}   // escrowed
Verdict     {task_id, agent_id, criteria[{name, pass, evidence, note}], overall, fix_list}
LedgerEntry {task_id, state: locked|released|refunded, amount, to}
Event       {task_id, type, payload, ts}   // the UI stream
```

Event types: `task_posted, no_supply, rubric_proposed, rubric_revised, rubric_confirmed, escrow_locked, candidates_found, assigned, deliverable_submitted, verdict, rerouted, settled, refunded`

Reputation scoring: Bayesian/Wilson-smoothed pass-rate weighted by task value (resists small-sample gaming).

---

## 8. Components

| Component | Model/Tech | Role |
|---|---|---|
| MCP server + REST API | HTTP | Product surface: 6 tools |
| Rubric compiler | Sonnet | spec → criteria; powers discussion thread |
| Orchestrator | Sonnet | decompose, select, assign, handle verdicts, reroute |
| Specialists ×2–3 | Haiku | labor; one seeded-bad for the demo arc |
| Verifier | Opus | rubric grading, evidence-quoted verdicts |
| Escrow ledger | in-memory module | lock / release / refund |
| Deliverable store | in-memory | escrowed work, released on settle |
| Reputation store | JSON | verdict-driven scores |
| Registry | seeded JSON, 3 agents | Agent Cards (A2A-inspired) |
| UI | single page, event stream | observability + demo theater |

---

## 9. UI (one screen, three zones)

- **Left — Marketplace:** agent cards (skill, price, rep, pass-rate); reputation ticks up/down live on verdicts.
- **Center — Live task feed (60%, the star):** streaming event cards — posted → rubric compiled → confirmed → escrow 🔒 → Agent A working → **verdict card ❌ (per-criterion fails + evidence quotes — the hero element)** → rerouting 🔁 → Agent B working → verdict ✅ → settled 💸.
- **Right — Ledger:** escrow states, per-agent balances. Good-to-have: "cost saved vs all-Opus" counter.
- Somewhere visible: `claude mcp add agent-market ...` snippet — the dashboard is the window, the endpoint is the product.

---

## 10. The Demo (seeded, ~90 sec)

Task: "Research + write a 200-word product brief on X", bounty ₹100 (mock).

1. Create → supply check ✓ → rubric compiled → (one live discussion round if time: "criterion 2 too strict" → revised) → confirm → escrow 🔒
2. Discovery → Agent A picked (highest rep/price)
3. Agent A (seeded-bad Haiku) submits sloppy work → Opus verdict **FAIL** with evidence quotes → payment withheld, rep drops, reroute
4. Agent B (good Haiku) submits → **PASS** → atomic flip: money → B, deliverable → buyer, rep ↑

The fail → withhold → reroute → pass → settle arc IS the pitch.

---

## 11. Build Plan (2 people, 5 hrs)

**First 15 min, together:** freeze contracts — data models + event schema above. Then never block each other.

- **Person A (the brain):** orchestrator, specialists (one seeded-bad), verifier + hardcoded rubric, demo scenario content.
- **Person B (the body):** ledger, reputation, registry seed, streaming UI (built against fake events from minute 15), recorded fallback + `--mock` mode.

| Time | Work |
|---|---|
| 0:00–0:15 | Together: contracts, scaffold, split |
| 0:15–2:00 | Parallel: A = agents+verifier; B = ledger+rep+UI (mock events) |
| 2:00–2:45 | Integration (budget real time; it always slips) |
| 2:45–3:30 | Seed + tune fail→reroute→pass until reliable |
| 3:30–4:15 | Polish, record fallback, mock mode |
| 4:15–5:00 | Pitch prep + submit (Devfolio MCP) |

**Rules:** trunk-only, small commits, two Claude Code sessions. Kill switch 3:15 — flaky loop → recorded demo. No refactors after 3:30. Ugly code that demos > clean code that doesn't.

**Hard OUT (roadmap slides only):** real payments/x402, auth, bidding, seller onboarding/qualification exams, manual selection, DB, sybil/collusion defenses, dispute resolution, multi-tenant.

**Good-to-haves if ahead (ranked):** cost-saved counter (~20m) · live rubric generate+confirm as real Sonnet calls (~20m) · comparative verification (~25m) · rep-weighted routing visibly changing picks (~15m) · injection-resistance demo (~30m) · stub MCP server connected live from your own Claude (~30m).

---

## 12. Judge Q&A (pre-empted)

- **"Isn't this just Anthropic Outcomes?"** — Outcomes is the grader. We're the settlement + cross-platform reputation economy around verification. The moat is the verified-outcome reputation graph, not the verifier.
- **"Can't the verifier be gamed?"** — Evidence-quoted rubrics, isolated verifier context, comparative scoring on the roadmap; rubric quality is platform-enforced at negotiation time.
- **"What if no agent can do the task?"** — Fail fast at create: `NO_SUPPLY`, suggest nearest capabilities, log as unmet demand → supply-recruitment flywheel.
- **"Can the buyer steal failed work?"** — No. Deliverables are escrowed; buyer sees verdicts, never failed work. Money and work swap atomically at settle.
- **"Why no bidding?"** — Price competition without trust is a lemon market. We rank by verified value; cold-start auction lane is roadmap.
- **End goal:** near-term, the quality gate enterprises need to ship multi-agent systems; long-term, the Moody's/Verisign of agent work — every agent's verified track record lives here, every A2A transaction settles through here. Take-rate on settlements; reputation graph = compounding data moat.
