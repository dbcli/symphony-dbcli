from __future__ import annotations

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from starlette.responses import Response

from symphony_dbcli.config import WorkflowConfig, default_config
from symphony_dbcli.store import Store

from .dependencies import STATIC_DIR, WebAppState
from .routers import api, ask, board, settings, sources, work_items, workers, workflow


def create_app(
    config: WorkflowConfig | None = None,
    store: Store | None = None,
    *,
    workflow_path: str = "WORKFLOW.md",
) -> FastAPI:
    active_config = config or default_config()
    active_store = store or Store(active_config.database.path)
    active_store.init()

    app = FastAPI(title="Symphony DBCLI")
    app.state.symphony = WebAppState(
        config=active_config,
        store=active_store,
        workflow_path=workflow_path,
    )
    app.mount("/web-static", StaticFiles(directory=str(STATIC_DIR)), name="web_static")

    app.include_router(board.router)
    app.include_router(sources.router)
    app.include_router(work_items.router)
    app.include_router(workers.router)
    app.include_router(workflow.router)
    app.include_router(ask.router)
    app.include_router(settings.router)
    app.include_router(api.router)

    @app.get("/favicon.ico", include_in_schema=False)
    def favicon() -> Response:
        return Response(status_code=204)

    return app
