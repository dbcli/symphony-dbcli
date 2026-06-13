from __future__ import annotations

import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from starlette.responses import Response

from symphony_dbcli.config import WorkflowConfig, default_config
from symphony_dbcli.db import create_db_engine, create_session_factory
from symphony_dbcli.models import create_model_tables
from symphony_dbcli.runtime import OrchestrationRuntime
from symphony_dbcli.sources import SourceSyncClient
from symphony_dbcli.store import Store

from .dependencies import STATIC_DIR, WebAppState, WebRuntime
from .routers import (
    api,
    ask,
    attempts,
    board,
    github_app,
    operations,
    settings,
    sources,
    work_items,
    workers,
    workflow,
)


def create_app(
    config: WorkflowConfig | None = None,
    store: Store | None = None,
    *,
    workflow_path: str = "WORKFLOW.md",
    source_sync_client: SourceSyncClient | None = None,
    runtime: WebRuntime | None = None,
    run_runtime: bool = False,
) -> FastAPI:
    active_config = config or default_config()
    active_store = store or Store(active_config.database.path)
    active_store.init()
    engine = create_db_engine(active_config.database.path)
    create_model_tables(engine)
    session_factory = create_session_factory(engine)

    active_runtime = runtime
    if active_runtime is None and run_runtime:
        active_runtime = OrchestrationRuntime(
            config=active_config,
            store=active_store,
            workflow_path=workflow_path,
            profile=active_config.profile.active,
        )

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        if active_runtime and run_runtime:
            active_runtime.start()
        try:
            yield
        finally:
            if active_runtime and run_runtime:
                active_runtime.stop()

    app = FastAPI(title="Symphony DBCLI", lifespan=lifespan)
    app.state.symphony = WebAppState(
        config=active_config,
        store=active_store,
        session_factory=session_factory,
        workflow_path=workflow_path,
        source_sync_client=source_sync_client,
        runtime=active_runtime,
    )
    app.mount("/web-static", StaticFiles(directory=str(STATIC_DIR)), name="web_static")

    app.include_router(board.router)
    app.include_router(sources.router)
    app.include_router(work_items.router)
    app.include_router(attempts.router)
    app.include_router(operations.router)
    app.include_router(workers.router)
    app.include_router(workflow.router)
    app.include_router(ask.router)
    app.include_router(settings.router)
    app.include_router(github_app.router)
    app.include_router(api.router)

    @app.get("/favicon.ico", include_in_schema=False)
    def favicon() -> Response:
        return Response(status_code=204)

    return app


def create_app_from_env() -> FastAPI:
    from symphony_dbcli.env import load_local_env

    load_local_env()
    workflow_path = os.environ.get("SYMPHONY_WORKFLOW", "WORKFLOW.md")
    profile = os.environ.get("SYMPHONY_PROFILE") or None
    run_runtime = os.environ.get("SYMPHONY_RUN_RUNTIME") == "1"

    from symphony_dbcli.config import load_workflow
    from symphony_dbcli.orchestrator import load_and_record_workflow

    config = load_workflow(workflow_path, profile=profile)
    store = Store(config.database.path)
    store.init()
    config, _version_id = load_and_record_workflow(store, workflow_path, profile=profile)
    return create_app(
        config,
        store,
        workflow_path=workflow_path,
        run_runtime=run_runtime,
    )
