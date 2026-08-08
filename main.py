"""Agent Market platform entrypoint. Run: uvicorn main:app --port 8000

One process, two surfaces over the same service layer:
  REST at /            (CONTRACTS wire formats; UI, workers, smoke test)
  MCP  at /mcp         (buyer agents: `claude mcp add --transport http ...`)
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse, HTMLResponse

import routes
from mcp_server import mcp

# Streamable HTTP sub-app; parent lifespan must run its session manager
# (Starlette does not run mounted sub-apps' lifespans).
_mcp_app = mcp.streamable_http_app(streamable_http_path="/", stateless_http=True)


@asynccontextmanager
async def lifespan(app: FastAPI):
    async with mcp.session_manager.run():
        yield


app = FastAPI(title="Agent Market", version="1.0", lifespan=lifespan)
routes.install_error_handler(app)
app.include_router(routes.router)
app.mount("/mcp", _mcp_app)

_UI_PATH = Path(__file__).parent / "ui" / "index.html"


@app.get("/", include_in_schema=False)
async def ui():
    if _UI_PATH.exists():
        return FileResponse(_UI_PATH)
    return HTMLResponse("<h1>Agent Market</h1><p>UI not built yet.</p>")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)
