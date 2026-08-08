"""Agent Market worker template — list yourself on the marketplace in one command.

    python agent_template.py --name my-agent --skills research writing \
        --price 50 --port 8010 --persona "You are a meticulous writer..."

Flow (CONTRACTS.md §3): on startup the agent registers itself with the platform,
then waits for work. The platform POSTs a job to /work; we accept instantly (202),
do the work with Claude in the background, and POST the result to callback_url.
"""
import argparse
import os
from contextlib import asynccontextmanager

import httpx
import uvicorn
from anthropic import AsyncAnthropic
from dotenv import load_dotenv
from fastapi import BackgroundTasks, FastAPI

load_dotenv()  # reads ANTHROPIC_API_KEY (and optional PLATFORM_URL) from .env

PLATFORM_URL = os.environ.get("PLATFORM_URL", "http://localhost:8000")
MODEL = "claude-haiku-4-5"


def build_agent(name: str, skills: list[str], price: int, port: int, persona: str) -> FastAPI:
    claude = AsyncAnthropic()
    card = {"id": "agt_unregistered"}  # filled in by registration

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        # §3.1 self-register; keep serving even if the platform isn't up yet
        try:
            async with httpx.AsyncClient() as http:
                resp = await http.post(f"{PLATFORM_URL}/agents", json={
                    "name": name, "skills": skills, "price": price,
                    "endpoint": f"http://localhost:{port}",
                })
                resp.raise_for_status()
                card.update(resp.json())
                print(f"[{name}] registered as {card['id']} (rep {card['rep_score']})", flush=True)
        except Exception as e:
            print(f"[{name}] registration failed ({e}) — serving anyway", flush=True)
        yield

    app = FastAPI(title=name, lifespan=lifespan)

    @app.post("/work", status_code=202)
    async def work(job: dict, background: BackgroundTasks):  # §3.2 dispatch
        background.add_task(do_work, job)
        return {"accepted": True}

    async def do_work(job: dict):
        rubric = "\n".join(f"- {r['criterion']}: {r['checkable_test']}" for r in job["rubric"])
        msg = await claude.messages.create(
            model=MODEL, max_tokens=2000, system=persona,
            messages=[{"role": "user", "content":
                f"TASK SPEC:\n{job['spec']}\n\nYou will be graded on this rubric:\n{rubric}"
                "\n\nProduce the deliverable now. Output ONLY the deliverable itself."}],
        )
        async with httpx.AsyncClient() as http:  # §3.3 deliver, echoing agent_token
            resp = await http.post(job["callback_url"], json={
                "agent_id": card["id"], "agent_token": job["agent_token"],
                "content": msg.content[0].text,
            })
            print(f"[{name}] delivered {job['task_id']} → {resp.status_code}", flush=True)

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
