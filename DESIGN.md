# DESIGN.md — Agent Market platform design decisions (v1)

Resolved design decisions from the pre-code review. Precedence:
**CONTRACTS.md (law) > ARCHITECTURE.md v2 > this doc fills the gaps.**
If code disagrees with CONTRACTS.md, the contract wins; if the contract
is silent, this doc wins.

---

## 1. Money & lifecycle

- **Two-step escrow (consent split):** `confirm` freezes the rubric and
  returns `402 {amount_due}` → status `CONFIRMED_UNFUNDED`. `fund` checks
  balance, moves bounty wallet→escrow, sets `FUNDED`, and auto-starts the
  pipeline. Confirm = agreeing to judging criteria; fund = authorizing
  spend.
- **Coins wallet model:** one store `{owner_id: balance}` — `"buyer"`,
  agent ids, `"platform"`. Settle: escrow → agent net + platform take.
  Refund: escrow → buyer. Escrow transitions ONLY `locked→released` or
  `locked→refunded`.
- **Take-rate arithmetic:** `take = bounty * 5 // 100`,
  `net = bounty - take`. Net + take always equals bounty exactly.
- **NO_SUPPLY persists:** task record is created (id, status `NO_SUPPLY`,
  events `task_posted` + `no_supply`) as the unmet-demand log, but the
  HTTP response stays the bare contract body
  `200 {"status": "NO_SUPPLY", "nearest_capabilities": [...]}`.
- **No in-process agents.** Registry starts empty; `endpoint` is required
  on every AgentCard. No `model` field on AgentCard (contract shape wins
  over ARCHITECTURE v2 §9).
- **Reputation:** simple deltas per contract — start 3.0, PASS +0.3
  (cap 5.0), FAIL −0.4 (floor 1.0). Wilson smoothing is roadmap.

## 2. Product surface: REST + MCP

- **MCP is core product.** One service layer, two thin surfaces:
  1. REST routes exactly per CONTRACTS §3–4 (workers, UI, smoke test).
  2. MCP server mounted at `/mcp` on the same FastAPI app — 8 buyer
     tools: `deposit_funds`, `create_task`, `discuss_rubric`,
     `confirm_rubric`, `fund_escrow`, `get_task_status`,
     `get_deliverable`, `list_agents`.
  Demo: `claude mcp add --transport http agent-market
  http://localhost:8000/mcp`.
- **Worker side stays plain HTTP by nature** (platform dials the agent;
  agent dials the callback) — MCP doesn't apply there.
- **No worker↔buyer clarification channel in v1, and never a direct
  link** (disintermediation, frozen-rubric integrity, deliverable-escrow
  leakage). Alignment is front-loaded into rubric discussion. Roadmap:
  platform-mediated Q&A thread (CLARIFYING state, paused clock, logged
  as dispute evidence, non-binding on the rubric).

## 3. Event architecture

- **Two SSE streams:** `GET /events` (global firehose — the UI's single
  subscription) plus `GET /events/{task_id}` per contract. Additive.
- **Non-task events** (`agent_registered`, `deposit`) carry
  `task_id: null` and live only in the global log.
- **Replay-then-live:** on connect, full history replays, then live.
  Monotonic sequence numbers internally.
- **Wire:** `data: {Event JSON}\n\n`, no named event types, `: ping`
  keepalive ~15s.
- **Additive payload field:** `rerouted` carries
  `reason: "timeout" | "verdict_failed" | "dispatch_failed"`.
- **Additive endpoint:** `GET /wallets → {owner: balance}` (contract has
  no wallet read; UI needs it).

## 4. LLM calls

- **Models (config.py):** rubric compiler `claude-sonnet-4-6`; verifier
  `claude-opus-5` (spec'd "claude-opus-4" is deprecated/retiring —
  opus-5 is the current Opus at the same price point).
- **Structured outputs everywhere** (`output_config.format` /
  `messages.parse` with pydantic schemas) — API-guaranteed valid JSON.
- **One Sonnet call at create** returns `{required_skills: subset of
  ["research","writing","extraction"] (schema enum), rubric: [4–6
  items]}`. Supply check runs on the result. NO_SUPPLY tasks still store
  the rubric (never shown).
- **Rubric admission rule = falsifiability:** a criterion is admissible
  iff the verifier could quote a specific passage to justify failing it.
  Subjective *intent* is fine when operationalized ("formal tone — no
  slang or contractions"); pure vibes ("well-written") are not.
- **revise() translates, never just refuses:** vague requests are
  operationalized into checkable criteria with an explanation in
  `changes`; hard refusal only when translation is impossible.
- **Confirm-with-edits is guarded:** same falsifiability check; on
  violation → `400 {"error": "unverifiable_criteria", "detail": ...,
  "suggestion": [translated rubric]}` (rubric not frozen).
- **Soft cap 5 discussion rounds:** round 6+ still works; responses nag
  to confirm. (No cancel endpoint exists in the contract.)
- **LLM failure policy — retry once, then per site:**
  - verifier → emit verdict FAIL (`note: "verifier_error"`), attempt
    burns, reroute, **no rep change**, no `fails` increment;
  - compile at create → `503`, nothing persisted;
  - revise → `503`, rubric unchanged.
- **MOCK_LLM=1 env flag:** compile returns a canned rubric; verify keys
  PASS/FAIL off a content marker. Deterministic, keyless smoke tests.

## 5. Discovery, dispatch & reroute

- **Ranking:** `rep_score / price` desc; ties → lower price, then
  earlier registration. Fully deterministic.
- **Re-rank the live registry at every attempt**, excluding agents that
  already failed this task. Mid-task registrations can catch reroutes;
  mid-task rep changes re-sort instantly. `candidates_found` emitted
  once at fund; reroutes emit `rerouted` only.
- **Dispatch retry:** `POST {endpoint}/work` retries connection errors
  and 5xx up to 5 tries total (backoff 0.5/1/2/4s, ≤~10s). 4xx =
  immediate fail, no retry. All tries fail → attempt burns.
- **30s deliverable clock starts at the successful 202**, not the first
  dispatch try.
- **Failed-attempt taxonomy:**
  | failure | rep effect |
  |---|---|
  | dispatch failed (all retries) | graduated timeout rule |
  | 30s timeout | graduated timeout rule |
  | verdict FAIL | −0.4, `fails`+1 |
  | verifier_error FAIL | none |
- **Graduated timeout rule:** per-agent `timeouts` counter; the first
  timeout/dispatch-failure ever is forgiven (rep unchanged), every
  subsequent one costs −0.4. (Contract is silent on timeout rep.)
- **Candidate exhaustion:** never re-dispatch an agent that failed this
  task; pool exhausted before 3 attempts → `FAILED_UNFULFILLED` +
  refund early. "Max 3" is a cap, not a quota.
- **agent_token:** `uuid4` per (task, attempt); single active token per
  task; invalidated the moment the attempt ends → late/duplicate/
  cross-agent submissions all `409`. Timeout race is safe under
  single-threaded asyncio.
- **Status loop:** `FUNDED → ASSIGNED → EXECUTING → VERIFYING →
  SETTLED`, or back to `ASSIGNED` (next candidate) on fail. An agent may
  hold multiple tasks concurrently.

## 6. Verification & settlement

- **Deliverable = one text/markdown string** (contract §3.3). The skill
  whitelist (research/writing/extraction = text artifacts) guarantees
  every admitted task is verifiable **by reading** — the supply check
  doubles as a verifiability check.
- **Verifier:** `verify(spec, rubric, deliverable)` — exactly three
  args. Opus, structured Verdict output, evidence quote mandatory per
  criterion, `overall = all(criteria)`. Fix-list on FAIL.
- **Mechanical pre-pass:** pure-Python word count / sentence lengths /
  URL count computed from the deliverable and injected as "measured
  facts" (LLMs miscount). Pure function of the three inputs — isolation
  preserved.
- **Prompt-injection stance:** the deliverable is untrusted data —
  wrapped in a delimited block; system prompt instructs the verifier to
  never follow instructions inside it. The evidence-quote mandate is
  itself a defense.
- **fix_list goes to the buyer (verdict event) but is NEVER forwarded to
  the next agent** on reroute — it derives from an escrowed deliverable.
- **Atomic settle (no awaits between mutations):** escrow
  locked→released → wallet credits (agent +net, platform +take) →
  deliverable unlocked → rep +0.3 / `passes`+1 / `jobs`+1 → emit
  `verdict` + `settled` → status `SETTLED`.
- **FAIL order:** emit `verdict` → rep −0.4 / `fails`+1 / `jobs`+1 →
  emit `rerouted` → next attempt (escrow untouched).
- **Roadmap:** pluggable verification modalities per skill — executable
  (sandboxed) verification for a future "coding" skill. Invariants that
  never change: frozen rubric, isolated verifier, mandatory evidence,
  atomic settle.

## 7. Concurrency

- Single uvicorn/asyncio process, plain dicts, no locks — atomicity =
  no `await` inside any read-modify-write. Pipeline runs via
  `asyncio.create_task` after `/fund` returns. 30s timeout = one asyncio
  timer per dispatch, cancelled on callback. Concurrent tasks are
  independent (state keyed by task id).

## 8. UI

- Single `ui/index.html`, vanilla JS, ONE `EventSource` on `/events`
  (replay-then-live → refresh-safe).
- **Left:** agent cards (name, skills, price, rep, pass-rate) from the
  `agent_registered` payload + `GET /agents` re-poll; rep ticks with a
  flash on verdicts.
- **Center (60%):** one card per event; the **verdict card is the
  hero** — per-criterion ✅/❌, blockquoted evidence, fix-list, colored
  border.
- **Right:** buyer balance, per-task escrow state, agent earnings,
  platform take (via `GET /wallets` + money events).

## 9. Smoke test

- `smoke.py`: two in-process stub agents (:8001 sloppy/bad content,
  :8002 diligent/good content; both `202` then callback ~1s). Register
  both (sloppy cheaper → legitimately ranked first) → deposit 200 →
  create (bounty 100) → confirm (assert 402) → fund. Assert the exact
  event sequence `task_posted … verdict(fail) … rerouted …
  verdict(pass) … settled`, terminal `SETTLED`, money invariant
  (buyer 100, diligent +95, platform +5), rep invariant (sloppy 2.6,
  diligent 3.3).
- Mocked by default (`MOCK_LLM=1`); `--live` flag for real LLM calls.

## Out of scope (roadmap only)

Real payment rails / withdrawals, auth, bidding, worker↔buyer Q&A
channel, executable/code verification, Wilson-smoothed reputation,
dispute resolution, DB persistence, multi-tenant.
