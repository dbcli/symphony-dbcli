from __future__ import annotations

from fastapi import APIRouter, Request

from symphony_dbcli.web.dependencies import get_app_state

router = APIRouter(prefix="/api", tags=["api"])


@router.get("/health")
def health(request: Request) -> dict[str, object]:
    state = get_app_state(request)
    return {
        "ok": True,
        "profile": state.config.profile.active,
        "database": state.config.database.path,
        "workflow_path": state.workflow_path,
    }
