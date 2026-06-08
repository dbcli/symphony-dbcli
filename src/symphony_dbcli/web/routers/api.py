from __future__ import annotations

import json
from collections.abc import AsyncIterator

import anyio
from fastapi import APIRouter, HTTPException, Request, status
from starlette.responses import StreamingResponse

from symphony_dbcli.store import AttemptLiveEvent, AttemptLiveSnapshot, Store
from symphony_dbcli.web.dependencies import get_app_state

router = APIRouter(prefix="/api", tags=["api"])
ATTEMPT_EVENT_POLL_SECONDS = 0.75
ATTEMPT_EVENT_HEARTBEAT_POLLS = 20


@router.get("/health")
def health(request: Request) -> dict[str, object]:
    state = get_app_state(request)
    return {
        "ok": True,
        "profile": state.config.profile.active,
        "database": state.config.database.path,
        "workflow_path": state.workflow_path,
    }


@router.get("/attempts/{attempt_id}/events")
def attempt_events(request: Request, attempt_id: int, once: bool = False) -> StreamingResponse:
    state = get_app_state(request)
    if not state.store.attempt_by_id(attempt_id):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Attempt not found")
    return StreamingResponse(
        _attempt_event_stream(request, state.store, attempt_id, once=once),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


async def _attempt_event_stream(
    request: Request,
    store: Store,
    attempt_id: int,
    *,
    once: bool,
) -> AsyncIterator[str]:
    after_codex_id = 0
    after_timeline_id = 0
    after_error_id = 0
    status_key = ""
    idle_polls = 0
    yield ": connected\n\n"
    while True:
        snapshot = store.attempt_live_events(
            attempt_id,
            after_codex_id=after_codex_id,
            after_timeline_id=after_timeline_id,
            after_error_id=after_error_id,
        )
        if snapshot is None:
            return

        sent_event = False
        next_status_key = f"{snapshot.status}:{snapshot.current_phase}:{snapshot.updated_at}"
        if next_status_key != status_key:
            status_key = next_status_key
            yield _sse_message(
                "attempt",
                _attempt_status_payload(snapshot),
                event_id=f"attempt-{snapshot.attempt_id}-{snapshot.updated_at}",
            )
            sent_event = True

        for event in snapshot.events:
            if event.source == "codex":
                after_codex_id = max(after_codex_id, event.id)
            elif event.source == "timeline":
                after_timeline_id = max(after_timeline_id, event.id)
            elif event.source == "error":
                after_error_id = max(after_error_id, event.id)
            yield _sse_message(
                event.source, _attempt_live_event_payload(event), event_id=f"{event.source}-{event.id}"
            )
            sent_event = True

        if once:
            return
        if await request.is_disconnected():
            return
        if sent_event:
            idle_polls = 0
        else:
            idle_polls += 1
            if idle_polls >= ATTEMPT_EVENT_HEARTBEAT_POLLS:
                idle_polls = 0
                yield ": heartbeat\n\n"
        await anyio.sleep(ATTEMPT_EVENT_POLL_SECONDS)


def _attempt_status_payload(snapshot: AttemptLiveSnapshot) -> dict[str, object]:
    return {
        "source": "attempt",
        "id": snapshot.attempt_id,
        "eventType": "status",
        "createdAt": snapshot.updated_at,
        "title": "Attempt status",
        "message": snapshot.status,
        "status": snapshot.status,
        "currentPhase": snapshot.current_phase,
    }


def _attempt_live_event_payload(event: AttemptLiveEvent) -> dict[str, object]:
    return {
        "source": event.source,
        "id": event.id,
        "eventType": event.event_type,
        "createdAt": event.created_at,
        "title": event.title,
        "message": event.message,
        "payload": event.payload,
        "outputDelta": event.output_delta,
    }


def _sse_message(event: str, data: dict[str, object], *, event_id: str) -> str:
    payload = json.dumps(data, separators=(",", ":"), sort_keys=True)
    return f"id: {event_id}\nevent: {event}\ndata: {payload}\n\n"
