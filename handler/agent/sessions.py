import asyncio
from datetime import datetime
from http import HTTPStatus

from fastapi.websockets import WebSocketState
from fastapi import WebSocket, WebSocketDisconnect, status as ws_status
from pydantic import ValidationError

from core.delegation.subagents import subscribe_session_events, unsubscribe_session_events
from core.runtime.session import get_agent_pool
from handler import cancel_ws_task as _cancel_task, close_ws_silently as _close_silently
from logger import get_logger
from middleware.auth import AuthUser, decode_access_token
from schema.agent.events import (
    AgentEventSchema,
    AgentInputPart,
    AgentStreamActionSchema,
    ErrorEvent,
    agent_stream_command_adapter,
)
from schema.agent.sessions import (
    CreateAgentSessionResponse,
    ListAgentEventsResponse,
    ListAgentSessionsResponse,
    UpdateAgentSessionTitleRequest,
)
from schema.common.responses import CommonResponse
from service.agent import runtime as agent_runtime
from service.agent import sessions as agent_sessions


logger = get_logger(__name__)


async def create_agent_session_handler(user: AuthUser) -> CommonResponse:
    session_id = await agent_sessions.create_session(user_id=user.id)
    return CommonResponse(data=CreateAgentSessionResponse(session_id=session_id))


async def delete_agent_session_handler(session_id: str, user: AuthUser) -> CommonResponse:
    deleted = await agent_sessions.delete_session(
        session_id,
        user_id=user.id,
        user_role=user.role,
    )
    if not deleted:
        return CommonResponse(code=HTTPStatus.NOT_FOUND.value, message="agent session not found")
    return CommonResponse(message="agent session deleted")


async def update_agent_session_title_handler(
    session_id: str,
    request: UpdateAgentSessionTitleRequest,
    user: AuthUser,
) -> CommonResponse:
    session = await agent_sessions.update_session_title(
        session_id=session_id,
        title=request.title,
        user_id=user.id,
        user_role=user.role,
    )
    if session is None:
        return CommonResponse(code=HTTPStatus.NOT_FOUND.value, message="agent session not found")
    return CommonResponse(message="agent session title updated", data=session)


async def list_agent_sessions_handler(limit: int, user: AuthUser) -> CommonResponse:
    sessions = await agent_sessions.list_sessions(
        limit=limit,
        user_id=user.id,
        user_role=user.role,
    )
    return CommonResponse(data=ListAgentSessionsResponse(items=sessions))


async def list_agent_events_handler(
    session_id: str,
    user: AuthUser,
    before_id: int | None = None,
    limit: int = agent_sessions.DEFAULT_REPLAY_EVENT_PAGE_SIZE,
) -> CommonResponse:
    result = await agent_sessions.replay_session_events_page(
        session_id=session_id,
        user_id=user.id,
        user_role=user.role,
        before_id=before_id,
        limit=limit,
    )
    if result is None:
        return CommonResponse(code=HTTPStatus.NOT_FOUND.value, message="agent session not found")
    events, has_more, next_before_id = result
    return CommonResponse(data=ListAgentEventsResponse(
        session_id=session_id,
        items=events,
        has_more=has_more,
        next_before_id=next_before_id,
    ))


async def handle_agent_stream(websocket: WebSocket, session_id: str, token: str) -> None:
    user = _decode_ws_token(token)
    if user is None:
        await websocket.close(code=ws_status.WS_1008_POLICY_VIOLATION)
        return
    if not await agent_sessions.can_access_session(session_id, user.id, user.role):
        await websocket.close(code=ws_status.WS_1008_POLICY_VIOLATION)
        return

    await websocket.accept()
    send_lock = asyncio.Lock()
    runtime = None
    runtime_events: asyncio.Queue[AgentEventSchema] | None = None
    subscriber: asyncio.Queue[AgentEventSchema] | None = None
    runtime_forwarder: asyncio.Task | None = None
    subagent_forwarder: asyncio.Task | None = None
    sent_events: set[str] = set()

    try:
        runtime, runtime_events = await get_agent_pool().subscribe(session_id)
        subscriber = await subscribe_session_events(session_id)
        runtime_forwarder = asyncio.create_task(_forward_events(
            websocket,
            runtime_events,
            send_lock,
            session_id,
            user,
            sent_events,
        ))
        subagent_forwarder = asyncio.create_task(_forward_events(
            websocket,
            subscriber,
            send_lock,
            session_id,
            user,
            sent_events,
        ))

        while True:
            try:
                payload = await websocket.receive_json()
            except WebSocketDisconnect:
                raise
            except Exception as exc:
                logger.info("agent stream rejected unreadable websocket payload: %s", exc)
                await _send_event(
                    websocket,
                    ErrorEvent(
                        created_at=datetime.now(),
                        message="Invalid websocket payload: expected a JSON stream command",
                        code="bad_request",
                    ),
                    send_lock,
                )
                await _send_event(websocket, agent_runtime.done_event(), send_lock)
                continue
            try:
                command = agent_stream_command_adapter.validate_python(payload)
            except ValidationError as exc:
                logger.info("agent stream rejected invalid payload: %s", _validation_error_message(exc))
                await _send_event(
                    websocket,
                    ErrorEvent(
                        created_at=datetime.now(),
                        message=f"Invalid message payload: {_validation_error_message(exc)}",
                        code="bad_request",
                    ),
                    send_lock,
                )
                await _send_event(websocket, agent_runtime.done_event(), send_lock)
                continue

            if command.action == AgentStreamActionSchema.INTERRUPT:
                await _interrupt_turn(websocket, session_id, user, send_lock)
                continue

            if command.action == AgentStreamActionSchema.CANCEL_ALL:
                await _cancel_all_tasks(websocket, session_id, user, send_lock)
                continue

            await _start_turn(
                websocket=websocket,
                session_id=session_id,
                content=command.content,
                user=user,
                sandbox_container_id=command.sandbox_container_id,
                requested_agent_code=command.agent_code,
                send_lock=send_lock,
            )
    except WebSocketDisconnect:
        pass
    except Exception:
        logger.exception("agent stream failed for session=%s", session_id)
        await _close_silently(websocket)
    finally:
        if runtime is not None and runtime_events is not None:
            runtime.unsubscribe(runtime_events)
        if subscriber is not None:
            await unsubscribe_session_events(session_id, subscriber)
        await _cancel_task(runtime_forwarder)
        await _cancel_task(subagent_forwarder)


async def _start_turn(
    websocket: WebSocket,
    session_id: str,
    content: list[AgentInputPart],
    user: AuthUser,
    sandbox_container_id: int | None,
    requested_agent_code: str | None,
    send_lock: asyncio.Lock,
) -> None:
    try:
        await agent_runtime.submit_turn(
            session_id=session_id,
            content=content,
            user=user,
            sandbox_container_id=sandbox_container_id,
            requested_agent_code=requested_agent_code,
        )
    except agent_runtime.SessionNotRunnableError:
        await _send_event(websocket, agent_runtime.not_runnable_error(), send_lock)
        await _send_event(websocket, agent_runtime.done_event(), send_lock)
    except PermissionError:
        await _send_event(websocket, agent_runtime.not_found_error(), send_lock)
        await _send_event(websocket, agent_runtime.done_event(), send_lock)
    except Exception as exc:
        logger.exception("agent turn failed for session=%s", session_id)
        await _send_event(websocket, ErrorEvent(created_at=datetime.now(), message=str(exc) or "agent turn failed"), send_lock)
        await _send_event(websocket, agent_runtime.done_event(), send_lock)


async def _interrupt_turn(
    websocket: WebSocket,
    session_id: str,
    user: AuthUser,
    send_lock: asyncio.Lock,
) -> None:
    try:
        interrupted = await agent_runtime.interrupt_turn(session_id=session_id, user=user)
    except PermissionError:
        await _send_event(websocket, agent_runtime.not_found_error(), send_lock)
        await _send_event(websocket, agent_runtime.done_event(), send_lock)
        return
    if not interrupted:
        await _send_event(websocket, agent_runtime.done_event(), send_lock)


async def _cancel_all_tasks(
    websocket: WebSocket,
    session_id: str,
    user: AuthUser,
    send_lock: asyncio.Lock,
) -> None:
    try:
        await agent_runtime.cancel_all_tasks(session_id=session_id, user=user)
    except PermissionError:
        await _send_event(websocket, agent_runtime.not_found_error(), send_lock)
    await _send_event(websocket, agent_runtime.done_event(), send_lock)


async def _send_event(
    websocket: WebSocket,
    event: AgentEventSchema,
    send_lock: asyncio.Lock | None = None,
) -> bool:
    if (
        websocket.client_state != WebSocketState.CONNECTED
        or websocket.application_state != WebSocketState.CONNECTED
    ):
        return False
    try:
        if send_lock is None:
            await websocket.send_text(event.model_dump_json())
        else:
            async with send_lock:
                await websocket.send_text(event.model_dump_json())
        return True
    except Exception:
        logger.debug("failed to send agent event to websocket", exc_info=True)
        return False


_ACCESS_CHECK_INTERVAL = 50

async def _forward_events(
    websocket: WebSocket,
    queue: asyncio.Queue[AgentEventSchema],
    send_lock: asyncio.Lock,
    session_id: str,
    user: AuthUser,
    sent_events: set[str],
) -> None:
    try:
        events_since_check = 0
        while True:
            event = await queue.get()
            events_since_check += 1
            if events_since_check >= _ACCESS_CHECK_INTERVAL:
                events_since_check = 0
                if not await agent_sessions.can_access_session(session_id, user.id, user.role):
                    await _close_silently(websocket, code=ws_status.WS_1008_POLICY_VIOLATION)
                    return
            key = _event_content_key(event)
            if key:
                if key in sent_events:
                    continue
                sent_events.add(key)
            if not await _send_event(websocket, event, send_lock):
                return
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.debug("agent event forwarding stopped", exc_info=True)


def _decode_ws_token(token: str) -> AuthUser | None:
    try:
        return decode_access_token(token)
    except Exception:
        return None


def _validation_error_message(exc: ValidationError) -> str:
    errors = exc.errors()
    if not errors:
        return str(exc)
    first = errors[0]
    loc = ".".join(str(part) for part in first.get("loc", ()) if part != "__root__")
    msg = str(first.get("msg") or "invalid payload")
    return f"{loc}: {msg}" if loc else msg


_KEY_SEP = "\x1f"


def _event_content_key(event: AgentEventSchema) -> str:
    """Lightweight content key for deduplicating events within a WS connection.

    Returns "" for control signals (done/run_state) that must never be deduplicated.
    """
    t = event.type
    nf = getattr(event, "nested_for", "")
    nc = getattr(event, "nested_call_id", "")
    s = _KEY_SEP
    if t in ("text_delta", "thinking_delta"):
        return ""
    if t in ("text_complete", "thinking_complete"):
        return f"{t}{s}{nf}{s}{nc}{s}{event.segment_id}{s}{event.text}"
    if t == "tool_call":
        return f"tool_call{s}{nf}{s}{nc}{s}{event.call_id}{s}{event.name}{s}{_stable_json(event.arguments)}"
    if t == "tool_result":
        return f"tool_result{s}{nf}{s}{nc}{s}{event.call_id}{s}{event.output}{s}{event.is_error}"
    if t == "subagent_task":
        return (
            f"subagent_task{s}{event.run_id}{s}{event.status}{s}{event.progress}"
            f"{s}{event.result}{s}{event.error}"
        )
    if t == "turn_boundary":
        return f"turn_boundary{s}{nf}{s}{nc}"
    if t == "user_message":
        return f"user_message{s}{event.display_text}"
    if t == "error":
        return f"error{s}{nf}{s}{nc}{s}{event.message}"
    return ""


def _stable_json(value) -> str:
    import json

    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    except TypeError:
        return str(value)
