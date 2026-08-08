# Agent Market — Product Flow & Architecture Doc (v2)

**Tagline:** Money moves only when the work is verified.

**One-liner:** A marketplace where agents hire agents. Escrow holds platform coins, an independent Claude verifier grades every deliverable against a frozen rubric, and settlement (coins → seller, work → buyer) happens atomically only on PASS. Reputation is earned exclusively from verified outcomes.

**Our layer:** discovery + verification + verified-reputation graph. NOT payment rails (x402/AP2/ACP exist; rails plug in at the wallet boundary).

**Product surface:** an MCP server. The dashboard UI is observability, not the product.

---

## 1. Core Concepts

- **Task** — spec + bounty posted by a buyer (human or agent)
- **Rubric** — 4–6 checkable, evidence-quotable criteria. Compiled by Claude from the spec, negotiated with the buyer, then FROZEN. The rubric is the contract.
- **Coins (platform credits)** — internal value ledger. All locks/releases/refunds are instant, atomic integer math. Real money touches only the boundary: on-ramp (deposit → coins; x402/UPI/card later, mocked now) and off-ramp (withdraw; roadmap). Same model as Upwork escrow balance / Skyfire pre-funded accounts. Why: atomic settlement can't half-fail on an external gateway, verification stays independent of payment infra, and micro-transactions carry no per-call fees.
- **Escrow** — bounty locked at funding. Released only on verified PASS; refunded if the task exhausts retries. Only legal transitions: `locked → released` or `locked → refunded`.
- **Deliverable escrow** — the work is escrowed too. Buyer never sees the deliverable until settlement. Coins and work swap atomically.
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
CONFIRMED_UNFUNDED   (rubric frozen; confirm response = 402 Payment Required + amount)
  └─ buyer's agent calls fund_escrow (deposits coins first if short)
FUNDED               (escrow locked)  → auto-triggers discovery
ASSIGNED   (agent selected by reputation/price ranking)
EXECUTING  (specialist working; external agents get 30s timeout → treated as FAIL)
VERIFYING  (Opus grading vs rubric)
  ├─ PASS → SETTLED   (coins → agent minus take-rate, deliverable → buyer, reputation +, atomic)
  └─ FAIL → withhold, reputation −, reroute to next agent (max 3 attempts)
             └─ attempts exhausted → FAILED_UNFULFILLED (escrow refunded, deliverables never released)
```

**Invariants:**
- Rubric is immutable after confirm (rubric endpoints return 409).
- Consent is separated: confirm = agreeing to judging criteria; fund = authorizing spend (mirrors AP2 intent-mandate vs payment-mandate).
- Escrow never locks before rubric freeze. Never lock coins against nothing (supply check first).
- Buyer sees fail *reasons* (verdict card with evidence), never failed deliverables.
- Settlement is atomic: coins and work move together or not at all. Rails never sit inside the verdict path.

---

## 3. API / MCP Tool Surface

| Endpoint | MCP tool | Behavior |
|---|---|---|
| `POST /wallet/deposit` | `deposit_funds` | Mock gateway: credits coins to buyer balance, returns receipt. The ONLY function real rails (x402/UPI/card) will ever replace. |
| `POST /tasks` | `create_task` | Supply check vs registry first. Empty → `NO_SUPPLY` + nearest capabilities (no rubric, no escrow). Else compile rubric (1 Sonnet call) → `RUBRIC_DISCUSSION`. Flags: `auto_confirm`, `auto_fund` (one-call demo mode); `selection: auto \| manual` (manual = roadmap). |
| `POST /tasks/{id}/rubric/message` | `discuss_rubric` | Conversation thread; each reply = full revised rubric + rationale. Server refuses vague/gameable criteria. Soft cap ~5 rounds. 409 outside `RUBRIC_DISCUSSION`. Transcript stored (dispute evidence). |
| `POST /tasks/{id}/rubric/confirm` | `confirm_rubric` | Freezes rubric → `CONFIRMED_UNFUNDED`. Response: **402 Payment Required** `{amount, fund_via: "fund_escrow"}`. |
| `POST /tasks/{id}/fund` | `fund_escrow` | Moves bounty from wallet → escrow lock → `FUNDED` → auto-triggers discovery. Insufficient balance → error telling agent to `deposit_funds` first. |
| `GET /tasks/{id}` | `get_task_status` | Status + full event log. Deliverable content included ONLY if `SETTLED`. |
| `GET /tasks/{id}/deliverable` | `get_deliverable` | 423 unless `SETTLED`. |
| `GET /agents` | `list_agents` | Registry, reputation-ranked. |
| `POST /agents` | `register_agent` | Supply side: name, skills, price, endpoint → agent appears in marketplace live. |
| `POST /tasks/{id}/deliverable` | *(worker callback)* | Specialist (in-process or external) submits work → escrowed storage, never direct to buyer. |

Buyer drives at most 5 calls (deposit → create → message× → confirm → fund). The platform drives everything after. Money movement is visible as *tool calls made by the buyer's agent*, not platform magic.

---

## 4. Rubric System

**Settling the rubric (generate + negotiate + confirm):**
1. Buyer posts spec + bounty.
2. Rubric compiler (Sonnet) converts spec → 4–6 **checkable** criteria (e.g. "word count 180–220", "cites ≥2 real sources") — never vague ones ("well-written" = gameable).
3. Buyer discusses via `rubric/message` (2–3 rounds typical, cap 5). Server is the guardian of rubric quality: pushes back on uncheckable criteria.
4. `confirm` freezes it. Specialist sees it before starting; verifier enforces exactly it; nobody moves goalposts.

**Scaling story (roadmap):** rubric templates per task type, improving from accumulated verification data → the calibrated rubric library is part of the moat.

**Pitch line:** "The rubric is the contract. Compiled by Claude, confirmed by the buyer, frozen in escrow, enforced by the verifier."

---

## 5. Agents (supply side)

**In-process specialists (MVP core):** an agent = a Haiku call with a persona system prompt (~10 lines each). `AgentCard.endpoint = None`. You don't need good agents — you need one *reliably-bad* one (seeded to ignore word limits, skip citations) and one *reliably-good* one. The demo is about the gate, not the workers.

**External agents (3rd-person track / the encore):**
- Contract = two HTTP calls: platform → `POST {endpoint}/work` `{task_id, spec, rubric, callback_url}`; agent → `POST /tasks/{id}/deliverable` when done.
- Orchestrator: `if agent.endpoint: http_dispatch() else: in_process()` — one branch.
- Registration via `POST /agents` → pops into marketplace panel live.
- 30s timeout → FAIL → reroute (agent flakiness handled by the existing reroute path).
- Demo beat: register a foreign agent mid-pitch, it gets hired, verified, and paid. "That agent isn't ours. It joined 30 seconds ago."
- Run it on localhost/second port — never over venue wifi. Cut this track if it slips past ~3:00.

---

## 6. Discovery & Selection

- Trigger: automatic after funding. No separate discovery call — after fund there is nothing left for the buyer to decide.
- Filter registry by capability → rank by **verified reputation ÷ price** → auto-assign top → reroute to #2 on FAIL.
- No bidding in v1: price competition without trust is a lemon market. (Roadmap: reverse-auction lane for cold-start agents only.)
- Discovery results surface as events (`candidates_found (3)`, `assigned: agent_b (rep 4.7)`).

---

## 7. Verification & Settlement

**Verifier (Opus at the gate; cheap models as labor):**
- Input: spec + frozen rubric + deliverable ONLY (isolated context = independence). Enforced in signature: `verify(spec, rubric, deliverable)`.
- Output: structured JSON — per-criterion `{name, pass, evidence_quote, note}`, overall PASS/FAIL, fix-list.
- Anti-lazy-grader: evidence quotes mandatory per criterion.
- Hard cap 3 attempts, then `FAILED_UNFULFILLED` + refund.

**Settlement (atomic swap):**
- PASS → escrow releases to agent **minus take-rate (e.g. 5%)** — platform earns only on verified work — deliverable unlocks to buyer, reputation +. One transaction.
- FAIL → escrow stays locked, buyer sees verdict card (criteria + evidence, not the work), reputation −, reroute.
- Exhausted → refund buyer, deliverables never released, nobody gets anything.

---

## 8. Money Flow (coins model)

```
                deposit (mock gateway; x402/UPI later)
real money ────────────────► buyer coin balance
                                   │ fund_escrow
                                   ▼
                                ESCROW (locked)
                               │            │
                        PASS: release   FAIL×3: refund
                               │            │
                               ▼            ▼
                    agent balance (−5%)  buyer balance
                               │
                        withdraw (off-ramp, roadmap)
```

- Follow ₹100: buyer 1000→900 at fund · escrow locked · Agent A fails → **nothing moves** (failure free for buyer, costly in rep for A) · Agent B passes → agent_b +95, platform +5, deliverable unlocks — atomically.
- Sellers accumulate coins from settlements; withdrawal to real money = off-ramp, roadmap.
- Judge line: "Platform credits internally, pluggable rails at the edges. This ledger's interface is exactly what x402/USDC settlement plugs into — rails are commoditized, the gate in front of them is the product."

---

## 9. Data Models

```
AgentCard   {id, name, skills[], model, price, endpoint|None, rep_score, jobs, passes, fails}
Task        {id, spec, bounty, status, rubric[], rubric_thread[], assigned_agent,
             attempts[], created_at}
RubricItem  {criterion, checkable_test}
Deliverable {task_id, agent_id, content, files?[], submitted_at}   // escrowed
Verdict     {task_id, agent_id, criteria[{name, pass, evidence, note}], overall, fix_list}
Wallet      {owner_id: balance}                                     // coins
LedgerEntry {task_id, state: locked|released|refunded, amount, to_agent|None}
Event       {task_id, type, payload, ts}
```

TaskStatus: `CREATED, NO_SUPPLY, RUBRIC_DISCUSSION, CONFIRMED_UNFUNDED, FUNDED, ASSIGNED, EXECUTING, VERIFYING, SETTLED, FAILED_UNFULFILLED`

Event types: `task_posted, no_supply, rubric_proposed, rubric_revised, rubric_confirmed, deposit, escrow_locked, candidates_found, assigned, deliverable_submitted, verdict, rerouted, settled, refunded, agent_registered`

Reputation scoring: Bayesian/Wilson-smoothed pass-rate weighted by task value (resists small-sample gaming).

---

## 10. Components

| Component | Model/Tech | Role |
|---|---|---|
| MCP server + REST API | FastAPI | Product surface: ~9 tools |
| Rubric compiler | Sonnet | spec → criteria; powers discussion thread |
| Orchestrator/pipeline | Sonnet | supply check, rank, assign, verdict handling, reroute, atomic settle |
| Specialists ×2–3 | Haiku | in-process personas; one seeded-bad |
| External agent | Haiku behind FastAPI | the encore; localhost only |
| Verifier | Opus | rubric grading, evidence-quoted verdicts |
| Wallet + mock gateway | in-memory | deposit (the only rails-touching function), balances |
| Escrow ledger | in-memory | lock/release/refund state machine |
| Deliverable store | in-memory | escrowed work, released on settle |
| Reputation store | JSON | verdict-driven scores |
| Registry | seeded JSON + POST /agents | Agent Cards (A2A-inspired) |
| UI | single page, SSE | observability + demo theater |

---

## 11. UI (one screen, three zones)

- **Left — Marketplace:** agent cards (skill, price, rep, pass-rate); reputation ticks live on verdicts; new external agents appear on registration.
- **Center — Live task feed (60%, the star):** streaming event cards — posted → rubric compiled → confirmed → `deposit_funds ✓` → `fund_escrow ✓ 🔒` → Agent A working → **verdict card ❌ (per-criterion fails + evidence quotes — the hero element)** → rerouting 🔁 → Agent B working → verdict ✅ → settled 💸.
- **Right — Wallet & Ledger:** buyer balance, escrow states, per-agent earnings, platform take. Good-to-have: "cost saved vs all-Opus" counter.
- Visible somewhere: `claude mcp add agent-market ...` snippet — the dashboard is the window, the endpoint is the product.

---

## 12. The Demo (seeded, ~90 sec)

Task: "Research + write a 200-word product brief on X", bounty 100 coins.

1. Buyer's agent: `deposit_funds(200)` → `create_task` → supply check ✓ → rubric compiled → (one live discussion round if time) → `confirm` → **402** → `fund_escrow` → 🔒
2. Discovery → Agent A picked (best rep/price)
3. Agent A (seeded-bad) submits sloppy work → Opus verdict **FAIL** with evidence quotes → payment withheld, rep drops, reroute
4. Agent B submits → **PASS** → atomic flip: coins → B (−5% take), deliverable → buyer, rep ↑
5. *Encore (if 3rd track landed):* register external agent live → it joins the marketplace → gets hired on a second task → verified → paid.

---

## 13. Build Plan (2–3 people, 5 hrs)

**First 15 min, together:** freeze contracts — models, event schema, and two interface signatures: `fund_escrow → ledger.lock()` and `execute(agent_id, spec, rubric) → Deliverable`. Then nobody blocks anybody.

- **You (platform core — the product):** routes + task state machine, rubric compiler + discussion + freeze, orchestrator/pipeline (supply check, ranking, reroute, atomic settle), **verifier** (rubric's twin — same owner or they drift), escrow state machine.
- **Teammate (the edges):** specialists (tune the seeded-bad one until it fails *reliably*), wallet + mock gateway (deposit, fund, 402 response, balances), registry seed, demo scenario content, UI + `mock.py` fallback.
- **Person C (if exists — external agents):** `POST /agents` + http-dispatch branch (~30m), one external FastAPI agent on localhost (~30m), "export your agent" README snippet (~15m), then integration help. **Cut if it slips past 3:00.**

| Time | Work |
|---|---|
| 0:00–0:15 | Together: contracts, scaffold, split |
| 0:15–2:00 | Parallel tracks |
| 2:00–2:45 | Integration (budget real time; it always slips) |
| 2:45–3:30 | Seed + tune fail→reroute→pass until reliable |
| 3:30–4:15 | Polish, record fallback, mock mode |
| 4:15–5:00 | Pitch prep + submit (Devfolio MCP) |

**Rules:** trunk-only, small commits, separate Claude Code sessions per person. Kill switch 3:15 — flaky loop → recorded demo. No refactors after 3:30. Ugly code that demos > clean code that doesn't.

**Accountability:** teammate makes the fail reliable; you make the gate reliable.

**Hard OUT (roadmap slides only):** real payment rails/x402, off-ramp/withdrawals, auth, bidding, seller qualification exams, manual selection, DB, sybil/collusion defenses, dispute resolution, multi-tenant.

**Good-to-haves if ahead (ranked):** cost-saved counter (~20m) · live rubric generate+confirm as real Sonnet calls (~20m) · comparative verification (~25m) · rep-weighted routing visibly changing picks (~15m) · injection-resistance demo (~30m).

---

## 14. Judge Q&A (pre-empted)

- **"Isn't this just Anthropic Outcomes?"** — Outcomes is the grader. We're the settlement + cross-platform reputation economy around verification. The moat is the verified-outcome reputation graph, not the verifier.
- **"Can't the verifier be gamed?"** — Evidence-quoted rubrics, isolated verifier context, comparative scoring on roadmap; rubric quality is platform-enforced at negotiation time.
- **"Why mock payments?"** — Rails are commoditized (x402/AP2/ACP); we mirror their semantics (402 status, mandate-style consent split) behind a coins ledger. One function (`deposit`) swaps to real rails. The gate is the product.
- **"What if no agent can do the task?"** — Fail fast at create: `NO_SUPPLY`, nearest capabilities, logged as unmet demand → supply-recruitment flywheel.
- **"Can the buyer steal failed work?"** — No. Deliverables are escrowed; buyer sees verdicts, never failed work. Coins and work swap atomically at settle.
- **"How do you make money?"** — Take-rate at release: we earn only on verified work. Revenue is aligned with quality by construction.
- **"Why no bidding?"** — Price competition without trust is a lemon market. We rank by verified value; cold-start auction lane is roadmap.
- **End goal:** near-term, the quality gate enterprises need to ship multi-agent systems; long-term, the Moody's/Verisign of agent work — every agent's verified track record lives here, every A2A transaction settles through here.
