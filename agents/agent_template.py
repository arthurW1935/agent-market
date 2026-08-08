"""Agent Market worker template - list yourself on the marketplace in one command.

    python agent_template.py --name my-agent --skills research writing \
        --price 50 --port 8010 --persona "You are a meticulous writer..."

Flow (CONTRACTS.md §3): on startup the agent registers itself with the platform,
then waits for work. The platform POSTs a job to /work; we accept instantly (202),
do the work with Claude in the background, and POST the result to callback_url.

A2A (additive, discovery-first):
  GET  /.well-known/agent-card.json  - public Agent Card (A2A 1.0)
  POST /a2a                          - JSON-RPC message/send (same labor as /work)
Platform hire path stays POST /work per CONTRACTS. Do not remove /work.
"""
import argparse
import os
import sys
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import httpx
import uvicorn
from anthropic import AsyncAnthropic
from dotenv import load_dotenv
from fastapi import BackgroundTasks, FastAPI, Request
from fastapi.responses import JSONResponse

# Windows consoles are often cp1252; force UTF-8 so logs don't crash on symbols.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

# Load .env from repo root (parent of agents/), then cwd as fallback.
_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(_ROOT / ".env")
load_dotenv()

PLATFORM_URL = os.environ.get("PLATFORM_URL", "http://localhost:8000")
MODEL = "claude-haiku-4-5"

# A2A AgentSkill metadata keyed by marketplace skill tag (CONTRACTS whitelist).
SKILL_META: dict[str, dict[str, Any]] = {
    "research": {
        "id": "research",
        "name": "Research",
        "description": "Gather facts and cite named, verifiable sources for briefs and analyses.",
        "tags": ["research"],
        "examples": [
            "Research competitors for product X and cite 2 sources",
            "Find recent facts about Y with named publications",
        ],
    },
    "writing": {
        "id": "writing",
        "name": "Writing",
        "description": "Write short-form product briefs and copy that satisfy a frozen rubric.",
        "tags": ["writing"],
        "examples": [
            "Write a 200-word product brief on X",
            "Draft a concise product description within a word limit",
        ],
    },
    "extraction": {
        "id": "extraction",
        "name": "Extraction",
        "description": "Extract structured fields from messy documents into valid JSON.",
        "tags": ["extraction"],
        "examples": [
            "Extract vendor, line items, and totals from this invoice text",
            "Parse a resume into structured requirements coverage JSON",
        ],
    },
}


def build_a2a_card(name: str, skills: list[str], port: int, persona: str) -> dict[str, Any]:
    """A2A 1.0 Agent Card served at /.well-known/agent-card.json."""
    desc = " ".join(persona.strip().split())
    if len(desc) > 280:
        desc = desc[:277] + "..."
    return {
        "name": name,
        "description": desc,
        "version": "1.0.0",
        "protocolVersion": "1.0",
        "url": f"http://localhost:{port}/a2a",
        "provider": {
            "organization": "Agent Market demo",
            "url": "http://localhost:8000",
        },
        "capabilities": {
            "streaming": False,
            "pushNotifications": False,
            "extendedAgentCard": False,
        },
        "defaultInputModes": ["application/json", "text/plain"],
        "defaultOutputModes": ["text/plain", "application/json"],
        "skills": [SKILL_META[s] for s in skills if s in SKILL_META],
    }


def _job_from_a2a_message(params: dict[str, Any]) -> dict[str, Any]:
    """Map A2A message/send params into CONTRACTS §3.2 /work job shape."""
    message = params.get("message") or {}
    parts = message.get("parts") or []
    data: dict[str, Any] = {}
    for part in parts:
        if not isinstance(part, dict):
            continue
        kind = part.get("kind") or part.get("type")
        if kind in ("data", "DataPart") and isinstance(part.get("data"), dict):
            data = part["data"]
            break
        if "data" in part and isinstance(part["data"], dict):
            data = part["data"]
            break

    task_id = data.get("market_task_id") or data.get("task_id") or str(uuid.uuid4())
    if "spec" not in data or "rubric" not in data or "callback_url" not in data:
        raise ValueError(
            "A2A data part must include spec, rubric, callback_url "
            "(and agent_token when calling from Agent Market)"
        )
    return {
        "task_id": task_id,
        "spec": data["spec"],
        "rubric": data["rubric"],
        "callback_url": data["callback_url"],
        "agent_token": data.get("agent_token", ""),
    }


def build_agent(name: str, skills: list[str], price: int, port: int, persona: str) -> FastAPI:
    if not (os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN")):
        print(f"[{name}] WARNING: ANTHROPIC_API_KEY missing - /work will accept but Claude will fail",
              flush=True)
    claude = AsyncAnthropic()
    card = {"id": "agt_unregistered"}  # marketplace card; filled in by registration
    a2a_card = build_a2a_card(name, skills, port, persona)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        # §3.1 self-register; keep serving even if the platform isn't up yet
        try:
            async with httpx.AsyncClient(timeout=5.0) as http:
                resp = await http.post(f"{PLATFORM_URL}/agents", json={
                    "name": name, "skills": skills, "price": price,
                    "endpoint": f"http://localhost:{port}",
                })
                resp.raise_for_status()
                card.update(resp.json())
                print(f"[{name}] registered as {card['id']} (rep {card['rep_score']})", flush=True)
        except Exception as e:
            print(f"[{name}] registration failed ({e}) - serving anyway", flush=True)
        print(f"[{name}] A2A card at http://localhost:{port}/.well-known/agent-card.json", flush=True)
        yield

    app = FastAPI(title=name, lifespan=lifespan)

    @app.get("/.well-known/agent-card.json")
    async def agent_card():
        """A2A discovery: public Agent Card (RFC 8615 well-known)."""
        return JSONResponse(a2a_card)

    @app.post("/work", status_code=202)
    async def work(job: dict, background: BackgroundTasks):  # §3.2 dispatch (CONTRACTS)
        background.add_task(do_work, job)
        return {"accepted": True}

    @app.post("/a2a")
    async def a2a_rpc(request: Request, background: BackgroundTasks):
        """Minimal A2A JSON-RPC: message/send -> same labor path as /work."""
        body = await request.json()
        req_id = body.get("id")
        method = body.get("method")
        if method not in ("message/send", "SendMessage", "message/stream"):
            return JSONResponse({
                "jsonrpc": "2.0",
                "id": req_id,
                "error": {"code": -32601, "message": f"Method not found: {method}"},
            }, status_code=200)
        if method == "message/stream":
            return JSONResponse({
                "jsonrpc": "2.0",
                "id": req_id,
                "error": {"code": -32004, "message": "Streaming not supported (capabilities.streaming=false)"},
            }, status_code=200)
        try:
            job = _job_from_a2a_message(body.get("params") or {})
        except ValueError as e:
            return JSONResponse({
                "jsonrpc": "2.0",
                "id": req_id,
                "error": {"code": -32602, "message": str(e)},
            }, status_code=200)

        a2a_task_id = str(uuid.uuid4())
        background.add_task(do_work, job)
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {
                "id": a2a_task_id,
                "contextId": job["task_id"],
                "status": {"state": "working"},
                "metadata": {
                    "market_task_id": job["task_id"],
                    "note": "Accepted; deliverable POSTed to callback_url when done (Agent Market async path).",
                },
            },
        }

    async def do_work(job: dict):
        task_id = job.get("task_id", "?")
        try:
            rubric = "\n".join(f"- {r['criterion']}: {r['checkable_test']}" for r in job["rubric"])
            msg = await claude.messages.create(
                model=MODEL, max_tokens=2000, system=persona,
                messages=[{"role": "user", "content":
                    f"TASK SPEC:\n{job['spec']}\n\nYou will be graded on this rubric:\n{rubric}"
                    "\n\nProduce the deliverable now. Output ONLY the deliverable itself."}],
            )
            content = msg.content[0].text.strip()
            if content.startswith("```"):  # models habitually fence raw output; unwrap it
                content = content.split("\n", 1)[1].rsplit("```", 1)[0].strip()
            async with httpx.AsyncClient(timeout=15.0) as http:  # §3.3 deliver, echoing agent_token
                resp = await http.post(job["callback_url"], json={
                    "agent_id": card["id"], "agent_token": job["agent_token"],
                    "content": content,
                })
                print(f"[{name}] delivered {task_id} -> {resp.status_code}", flush=True)
        except Exception as e:
            # Background failures must not become unhandled ASGI crashes.
            print(f"[{name}] work failed for {task_id}: {type(e).__name__}: {e}", flush=True)

    return app


if __name__ == "__main__":
    p = argparse.ArgumentParser(description="Run an Agent Market worker agent")
    p.add_argument("--name", required=True)
    p.add_argument("--skills", nargs="+", required=True, choices=["research", "writing", "extraction"])
    p.add_argument("--price", type=int, required=True)
    p.add_argument("--port", type=int, required=True)
    p.add_argument("--persona", required=True, help="system prompt defining how this agent works")
    args = p.parse_args()
    uvicorn.run(build_agent(args.name, args.skills, args.price, args.port, args.persona),
                host="127.0.0.1", port=args.port)
