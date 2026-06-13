from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated

from fastapi import APIRouter, Form, HTTPException, Request, status
from starlette.responses import RedirectResponse, Response

from symphony_dbcli.github import GitHubClient
from symphony_dbcli.sources import (
    SourceCreate,
    SourceSyncService,
    SourceUpdate,
    SourceValidationError,
    source_filters_from_form,
)
from symphony_dbcli.web.dependencies import get_app_state, page_context, source_repository, templates

router = APIRouter(tags=["sources"])


@dataclass(frozen=True)
class SourceEditForm:
    display_name: str
    enabled: bool
    labels: str
    authors: str
    updated_after: str
    updated_before: str
    stale_after_days: str


@router.get("/sources")
def index(request: Request) -> Response:
    context = page_context(request, title="Sources", active="sources")
    context["sources"] = source_repository(request).list_sources()
    return templates.TemplateResponse(
        request=request,
        name="sources/index.html",
        context=context,
    )


@router.get("/sources/new")
def new(request: Request) -> Response:
    context = page_context(request, title="Add Source", active="sources")
    context["repo"] = ""
    context["error"] = ""
    return templates.TemplateResponse(
        request=request,
        name="sources/new.html",
        context=context,
    )


@router.post("/sources")
def create(request: Request, repo: Annotated[str, Form()]) -> Response:
    try:
        source_repository(request).create_source(SourceCreate(repo=repo))
    except SourceValidationError as exc:
        context = page_context(request, title="Add Source", active="sources")
        context["repo"] = repo
        context["error"] = str(exc)
        return templates.TemplateResponse(
            request=request,
            name="sources/new.html",
            context=context,
            status_code=status.HTTP_400_BAD_REQUEST,
        )
    return RedirectResponse("/sources", status_code=status.HTTP_303_SEE_OTHER)


@router.get("/sources/{source_id}/edit")
def edit(request: Request, source_id: int) -> Response:
    source = source_repository(request).get_source(source_id)
    if source is None:
        raise HTTPException(status_code=404, detail="Source not found")
    context = page_context(request, title="Edit Source", active="sources")
    context["source"] = source
    context["form"] = SourceEditForm(
        display_name=source.display_name,
        enabled=source.enabled,
        labels=source.filters.labels_text,
        authors=source.filters.authors_text,
        updated_after=source.filters.updated_after,
        updated_before=source.filters.updated_before,
        stale_after_days=""
        if source.filters.stale_after_days is None
        else str(source.filters.stale_after_days),
    )
    context["error"] = ""
    return templates.TemplateResponse(
        request=request,
        name="sources/edit.html",
        context=context,
    )


@router.get("/sources/{source_id}/delete")
def delete_confirmation(request: Request, source_id: int) -> Response:
    source = source_repository(request).get_source(source_id)
    if source is None:
        raise HTTPException(status_code=404, detail="Source not found")
    context = page_context(request, title="Delete Source", active="sources")
    context["source"] = source
    context["confirmation"] = ""
    context["error"] = ""
    return templates.TemplateResponse(
        request=request,
        name="sources/delete.html",
        context=context,
    )


@router.post("/sources/{source_id}/delete")
def delete(
    request: Request,
    source_id: int,
    confirmation: Annotated[str, Form()],
) -> Response:
    repo = source_repository(request)
    source = repo.get_source(source_id)
    if source is None:
        raise HTTPException(status_code=404, detail="Source not found")
    if confirmation.strip() != source.display_name:
        context = page_context(request, title="Delete Source", active="sources")
        context["source"] = source
        context["confirmation"] = confirmation
        context["error"] = f"Type {source.display_name} to confirm deletion."
        return templates.TemplateResponse(
            request=request,
            name="sources/delete.html",
            context=context,
            status_code=status.HTTP_400_BAD_REQUEST,
        )
    repo.delete_source(source_id)
    return RedirectResponse("/sources", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/sources/{source_id}")
def update(
    request: Request,
    source_id: int,
    display_name: Annotated[str, Form()],
    labels: Annotated[str, Form()] = "",
    authors: Annotated[str, Form()] = "",
    updated_after: Annotated[str, Form()] = "",
    updated_before: Annotated[str, Form()] = "",
    stale_after_days: Annotated[str, Form()] = "",
    enabled: Annotated[bool, Form()] = False,
) -> Response:
    repo = source_repository(request)
    source = repo.get_source(source_id)
    if source is None:
        raise HTTPException(status_code=404, detail="Source not found")
    try:
        repo.update_source(
            source_id,
            SourceUpdate(
                display_name=display_name,
                enabled=enabled,
                filters=source_filters_from_form(
                    labels=labels,
                    authors=authors,
                    updated_after=updated_after,
                    updated_before=updated_before,
                    stale_after_days=stale_after_days,
                ),
            ),
        )
    except SourceValidationError as exc:
        context = page_context(request, title="Edit Source", active="sources")
        context["source"] = source
        context["form"] = SourceEditForm(
            display_name=display_name,
            enabled=enabled,
            labels=labels,
            authors=authors,
            updated_after=updated_after,
            updated_before=updated_before,
            stale_after_days=stale_after_days,
        )
        context["error"] = str(exc)
        return templates.TemplateResponse(
            request=request,
            name="sources/edit.html",
            context=context,
            status_code=status.HTTP_400_BAD_REQUEST,
        )
    return RedirectResponse("/sources", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/sources/{source_id}/sync")
def sync(request: Request, source_id: int) -> Response:
    state = get_app_state(request)
    client = state.source_sync_client or GitHubClient(state.config.github)
    service = SourceSyncService(source_repository(request), client)
    try:
        service.sync_source(source_id)
    except SourceValidationError:
        return RedirectResponse("/sources", status_code=status.HTTP_303_SEE_OTHER)
    except RuntimeError:
        return RedirectResponse(
            f"/board/source/{source_id}?sync=failed", status_code=status.HTTP_303_SEE_OTHER
        )
    return RedirectResponse(
        f"/board/source/{source_id}?sync=succeeded", status_code=status.HTTP_303_SEE_OTHER
    )
