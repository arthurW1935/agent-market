"""Agent Market platform entrypoint. Run: uvicorn main:app --port 8000"""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse, HTMLResponse

import routes

app = FastAPI(title="Agent Market", version="1.0")
routes.install_error_handler(app)
app.include_router(routes.router)

_UI_PATH = Path(__file__).parent / "ui" / "index.html"


@app.get("/", include_in_schema=False)
def ui():
    if _UI_PATH.exists():
        return FileResponse(_UI_PATH)
    return HTMLResponse("<h1>Agent Market</h1><p>UI not built yet.</p>")


try:  # MCP surface (step 4); platform works without it if deps are missing
    from mcp_server import mount_mcp
    mount_mcp(app)
except ImportError:
    pass


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)
