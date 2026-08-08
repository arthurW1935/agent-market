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
# DNS-rebinding protection off: the default only allows localhost Host headers,
# which 421s any tunneled/deployed access to /mcp (ngrok, fly, ...). This is a
# public marketplace endpoint, not a local-only server.
from mcp.server.transport_security import TransportSecuritySettings

_mcp_app = mcp.streamable_http_app(
    streamable_http_path="/",
    stateless_http=True,
    transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    async with mcp.session_manager.run():
        yield


app = FastAPI(title="Agent Market", version="1.0", lifespan=lifespan)
routes.install_error_handler(app)
app.include_router(routes.router)
app.mount("/mcp", _mcp_app)

_UI_DIR = Path(__file__).parent / "ui"


@app.get("/", include_in_schema=False)
async def landing():
    page = _UI_DIR / "landing.html"
    if page.exists():
        return FileResponse(page)
    return HTMLResponse("<h1>Agent Market</h1><p><a href='/dashboard'>dashboard</a></p>")


@app.get("/dashboard", include_in_schema=False)
async def dashboard():
    page = _UI_DIR / "index.html"
    if page.exists():
        return FileResponse(page)
    return HTMLResponse("<h1>Agent Market</h1><p>UI not built yet.</p>")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)
