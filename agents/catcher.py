"""Test helper: impersonates the platform's deliverable callback (CONTRACTS §3.3).

Run on :9999, point a fake dispatch's callback_url here, and see what the agent
delivers — lets us test any agent standalone, no platform needed.

    python catcher.py
"""
import uvicorn
from fastapi import FastAPI

app = FastAPI(title="callback-catcher")


@app.post("/tasks/{task_id}/deliverable", status_code=202)
async def catch(task_id: str, body: dict):
    print(f"\n=== DELIVERABLE for {task_id} from {body.get('agent_id')} "
          f"(token: {body.get('agent_token')}) ===")
    print(body.get("content"))
    print(f"=== {len(str(body.get('content', '')).split())} words ===\n")
    return {"received": True}


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=9999)
