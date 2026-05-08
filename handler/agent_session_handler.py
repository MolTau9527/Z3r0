import asyncio
from datetime import datetime
import shlex
from http import HTTPStatus
from collections.abc import Callable

from fastapi.websockets import WebSocketState
from fastapi import WebSocket, WebSocketDisconnect, status as ws_status
from pydantic import ValidationError

from core.subordinates import subscribe_session_events, unsubscribe_session_events
from core.context import AgentRuntimeContext, AgentUserContext
from core.runtime import get_agent_pool
from core.tools import SANDBOX_SKILLS_DIR
from logger import get_logger
from middleware.auth import AuthUser, decode_access_token
from schema.agent_event_schema import (
    AgentEventSchema,
    AgentStreamActionSchema,
    DoneEvent,
    ErrorEvent,
    agent_stream_command_adapter,
)
from schema.agent_session_schema import (
    CreateAgentSessionResponse,
    ListAgentEventsResponse,
    ListAgentSessionsResponse,
)
from schema.response_schema import CommonResponse
from service import agent_session_service
from service.sandbox_container_service import resolve_sandbox_container_tool_binding
from service.sandbox_container_service import execute_sandbox_container_command


logger = get_logger(__name__)

_MAX_SANDBOX_SKILLS = 32


async def create_agent_session_handler(user: AuthUser) -> CommonResponse:
    session_id = await agent_session_service.create_session(user_id=user.id)
    return CommonResponse(data=CreateAgentSessionResponse(session_id=session_id))


async def delete_agent_session_handler(session_id: str, user: AuthUser) -> CommonResponse:
    deleted = await agent_session_service.delete_session(
        session_id,
        user_id=user.id,
        user_role=user.role,
    )
    if not deleted:
        return CommonResponse(code=HTTPStatus.NOT_FOUND.value, message="agent session not found")
    return CommonResponse(message="agent session deleted")


async def list_agent_sessions_handler(limit: int, user: AuthUser) -> CommonResponse:
    sessions = await agent_session_service.list_sessions(
        limit=limit,
        user_id=user.id,
        user_role=user.role,
    )
    return CommonResponse(data=ListAgentSessionsResponse(items=sessions))


async def list_agent_events_handler(session_id: str, user: AuthUser) -> CommonResponse:
    events = await agent_session_service.replay_session_events(
        session_id=session_id,
        user_id=user.id,
        user_role=user.role,
    )
    if events is None:
        return CommonResponse(code=HTTPStatus.NOT_FOUND.value, message="agent session not found")
    return CommonResponse(data=ListAgentEventsResponse(session_id=session_id, items=events))


async def handle_agent_stream(websocket: WebSocket, session_id: str, token: str) -> None:
    user = _decode_ws_token(token)
    if user is None:
        await websocket.close(code=ws_status.WS_1008_POLICY_VIOLATION)
        return
    if not await agent_session_service.can_access_session(session_id, user.id, user.role):
        await websocket.close(code=ws_status.WS_1008_POLICY_VIOLATION)
        return

    await websocket.accept()
    runner: asyncio.Task | None = None
    notification_runner: asyncio.Task | None = None
    send_lock = asyncio.Lock()
    subscriber = await subscribe_session_events(session_id)

    def _set_notification_runner(task: asyncio.Task | None) -> None:
        nonlocal notification_runner
        notification_runner = task

    forwarder = asyncio.create_task(_forward_subagent_events(
        websocket,
        session_id,
        subscriber,
        user,
        send_lock,
        lambda: runner,
        lambda: notification_runner,
        lambda task: _set_notification_runner(task),
    ))

    _schedule_notification_drain(
        websocket,
        session_id,
        user,
        send_lock,
        lambda: runner,
        lambda: notification_runner,
        _set_notification_runner,
    )

    try:
        while True:
            payload = await websocket.receive_json()
            try:
                command = agent_stream_command_adapter.validate_python(payload)
            except ValidationError:
                logger.info("agent stream ignored invalid payload: %r", payload)
                continue

            if command.action == AgentStreamActionSchema.INTERRUPT:
                had_runner = runner is not None and not runner.done()
                if had_runner:
                    await _interrupt_turn(websocket, session_id, runner, user, send_lock)
                    runner = None
                elif notification_runner is not None and not notification_runner.done():
                    await _cancel_task(notification_runner)
                    notification_runner = None
                    await _send_event(websocket, DoneEvent(created_at=datetime.now()), send_lock)
                else:
                    await _interrupt_turn(websocket, session_id, runner, user, send_lock)
                continue

            if command.action == AgentStreamActionSchema.CANCEL_ALL:
                await _cancel_all_tasks(websocket, session_id, runner, notification_runner, user, send_lock)
                runner = None
                notification_runner = None
                continue

            text = command.text.strip()
            if not text:
                continue
            if runner is not None and not runner.done():
                await get_agent_pool().try_interrupt(session_id)
            await _cancel_task(runner)
            await _cancel_task(notification_runner)
            notification_runner = None
            runner = asyncio.create_task(_run_turn(
                websocket=websocket,
                session_id=session_id,
                text=text,
                user=user,
                sandbox_container_id=command.sandbox_container_id,
                requested_agent_code=command.agent_code,
                send_lock=send_lock,
                on_finished=lambda: _schedule_notification_drain(
                    websocket,
                    session_id,
                    user,
                    send_lock,
                    lambda: runner,
                    lambda: notification_runner,
                    _set_notification_runner,
                ),
            ))
    except WebSocketDisconnect:
        await _cancel_task(runner)
        await _cancel_task(notification_runner)
    except Exception:
        logger.exception("agent stream failed for session=%s", session_id)
        await _cancel_task(runner)
        await _cancel_task(notification_runner)
        await _close_silently(websocket)
    finally:
        await unsubscribe_session_events(session_id, subscriber)
        await _cancel_task(forwarder)


async def _run_turn(
    websocket: WebSocket,
    session_id: str,
    text: str,
    user: AuthUser,
    sandbox_container_id: int | None,
    requested_agent_code: str | None,
    send_lock: asyncio.Lock,
    on_finished,
) -> None:
    # always emits a DoneEvent so the client exits streaming
    saw_done = False
    try:
        agent_code = await agent_session_service.ensure_chat_session_meta(
            session_id,
            text,
            requested_agent_code,
            user_id=user.id,
            user_role=user.role,
        )
        context = await _build_runtime_context(session_id, user, sandbox_container_id)
        runtime = get_agent_pool().get_or_create(session_id)
        async for event in runtime.stream_turn(text, agent_code, context):
            if isinstance(event, DoneEvent):
                saw_done = True
            if not await _send_event(websocket, event, send_lock):
                return
    except asyncio.CancelledError:
        raise
    except PermissionError:
        await _send_event(websocket, ErrorEvent(created_at=datetime.now(), message="agent session not found", code="not_found"), send_lock)
    except Exception as exc:
        logger.exception("agent turn failed for session=%s", session_id)
        await _send_event(websocket, ErrorEvent(created_at=datetime.now(), message=str(exc) or "agent turn failed"), send_lock)
    finally:
        if not saw_done:
            await _send_event(websocket, DoneEvent(created_at=datetime.now()), send_lock)
        asyncio.get_running_loop().call_soon(on_finished)


async def _interrupt_turn(
    websocket: WebSocket,
    session_id: str,
    runner: asyncio.Task | None,
    user: AuthUser,
    send_lock: asyncio.Lock,
) -> None:
    if not await agent_session_service.can_access_session(session_id, user.id, user.role):
        await _send_event(websocket, ErrorEvent(created_at=datetime.now(), message="agent session not found", code="not_found"), send_lock)
        await _send_event(websocket, DoneEvent(created_at=datetime.now()), send_lock)
        return

    had_local_runner = runner is not None and not runner.done()
    interrupted = await get_agent_pool().try_interrupt(session_id)
    await _cancel_task(runner)
    if interrupted and not had_local_runner:
        await _send_event(websocket, DoneEvent(created_at=datetime.now()), send_lock)


async def _cancel_all_tasks(
    websocket: WebSocket,
    session_id: str,
    runner: asyncio.Task | None,
    notification_runner: asyncio.Task | None,
    user: AuthUser,
    send_lock: asyncio.Lock,
) -> None:
    if not await agent_session_service.can_access_session(session_id, user.id, user.role):
        await _send_event(websocket, ErrorEvent(created_at=datetime.now(), message="agent session not found", code="not_found"), send_lock)
        await _send_event(websocket, DoneEvent(created_at=datetime.now()), send_lock)
        return

    await _cancel_task(runner)
    await _cancel_task(notification_runner)
    await get_agent_pool().cancel_all(session_id)
    await _send_event(websocket, DoneEvent(created_at=datetime.now()), send_lock)


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


async def _forward_subagent_events(
    websocket: WebSocket,
    session_id: str,
    queue: asyncio.Queue[AgentEventSchema],
    user: AuthUser,
    send_lock: asyncio.Lock,
    get_runner: Callable[[], asyncio.Task | None],
    get_notification_runner: Callable[[], asyncio.Task | None],
    set_notification_runner: Callable[[asyncio.Task | None], None],
) -> None:
    try:
        while True:
            event = await queue.get()
            if not await _send_event(websocket, event, send_lock):
                return
            if _is_terminal_subagent_event(event):
                _schedule_notification_drain(
                    websocket,
                    session_id,
                    user,
                    send_lock,
                    get_runner,
                    get_notification_runner,
                    set_notification_runner,
                )
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.debug("subagent event forwarding stopped session=%s", session_id, exc_info=True)


def _schedule_notification_drain(
    websocket: WebSocket,
    session_id: str,
    user: AuthUser,
    send_lock: asyncio.Lock,
    get_runner: Callable[[], asyncio.Task | None],
    get_notification_runner: Callable[[], asyncio.Task | None],
    set_notification_runner: Callable[[asyncio.Task | None], None],
) -> None:
    local_runner = get_runner()
    if local_runner is not None and not local_runner.done():
        return
    current = get_notification_runner()
    if current is not None and not current.done():
        return
    task = asyncio.create_task(
        _drain_notifications(
            websocket=websocket,
            session_id=session_id,
            user=user,
            send_lock=send_lock,
            get_runner=get_runner,
            set_notification_runner=set_notification_runner,
        ),
        name=f"agent-notifications-{session_id}",
    )
    set_notification_runner(task)


async def _drain_notifications(
    *,
    websocket: WebSocket,
    session_id: str,
    user: AuthUser,
    send_lock: asyncio.Lock,
    get_runner: Callable[[], asyncio.Task | None],
    set_notification_runner: Callable[[asyncio.Task | None], None],
) -> None:
    try:
        if not await agent_session_service.can_access_session(session_id, user.id, user.role):
            return
        runtime = get_agent_pool().get_or_create(session_id)
        while True:
            local_runner = get_runner()
            if local_runner is not None and not local_runner.done():
                return
            if not await runtime.has_pending_notification():
                return
            context = _build_base_runtime_context(session_id, user)
            saw_done = False
            async for event in runtime.stream_notification(context):
                if isinstance(event, DoneEvent):
                    saw_done = True
                if not await _send_event(websocket, event, send_lock):
                    return
            if not saw_done:
                await _send_event(websocket, DoneEvent(created_at=datetime.now()), send_lock)
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.exception("agent notification drain failed for session=%s", session_id)
        await _send_event(websocket, ErrorEvent(created_at=datetime.now(), message="agent notification handling failed"), send_lock)
        await _send_event(websocket, DoneEvent(created_at=datetime.now()), send_lock)
    finally:
        set_notification_runner(None)


def _is_terminal_subagent_event(event: AgentEventSchema) -> bool:
    return (
        getattr(event, "type", "") == "subagent_task"
        and getattr(event, "status", "") in {"completed", "failed", "canceled"}
    )


async def _cancel_task(task: asyncio.Task | None) -> None:
    if task is None or task.done():
        return
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


async def _close_silently(websocket: WebSocket) -> None:
    try:
        await websocket.close(code=ws_status.WS_1011_INTERNAL_ERROR)
    except Exception:
        pass


async def _build_runtime_context(
    session_id: str,
    user: AuthUser,
    sandbox_container_id: int | None,
) -> AgentRuntimeContext:
    selected_container_id = None
    selected_container_generation = 0
    sandbox_skill_metadata: tuple[str, ...] = ()
    if sandbox_container_id is not None:
        binding = await resolve_sandbox_container_tool_binding(
            id=sandbox_container_id,
            user_id=user.id,
            user_role=user.role,
        )
        if binding is not None:
            selected_container_id = binding.id
            selected_container_generation = binding.generation
            sandbox_skill_metadata = await _load_sandbox_skill_metadata(binding.id)

    return AgentRuntimeContext(
        session_id=session_id,
        user=_agent_user_context(user),
        sandbox_container_id=selected_container_id,
        sandbox_container_generation=selected_container_generation,
        sandbox_skill_metadata=sandbox_skill_metadata,
    )


def _build_base_runtime_context(session_id: str, user: AuthUser) -> AgentRuntimeContext:
    return AgentRuntimeContext(session_id=session_id, user=_agent_user_context(user))


def _agent_user_context(user: AuthUser) -> AgentUserContext:
    return AgentUserContext(
        id=user.id,
        username=user.username,
        email=user.email,
        role=user.role,
    )


async def _load_sandbox_skill_metadata(container_id: int) -> tuple[str, ...]:
    try:
        result = await execute_sandbox_container_command(
            id=container_id,
            command=_build_skill_metadata_command(),
        )
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.debug("failed to load sandbox skill metadata: %s", container_id, exc_info=True)
        return ()
    if result.exit_code != 0 or not result.output.strip():
        return ()
    return tuple(_parse_skill_metadata_output(result.output))


def _build_skill_metadata_command() -> str:
    skills_dir = shlex.quote(SANDBOX_SKILLS_DIR)
    return f"""
if [ -d {skills_dir} ]; then
  find {skills_dir} -mindepth 2 -maxdepth 2 -name SKILL.md -type f | sort | head -n {_MAX_SANDBOX_SKILLS} | while IFS= read -r skill_file; do
    skill_name=$(basename "$(dirname "$skill_file")")
    printf '===SKILL:%s===\n' "$skill_name"
    awk '
      NR == 1 && $0 == "---" {{ print; in_fm = 1; next }}
      in_fm {{ print; if ($0 == "---") exit }}
    ' "$skill_file"
  done
fi
""".strip()


def _parse_skill_metadata_output(output: str) -> list[str]:
    blocks: list[str] = []
    current_name = ""
    current_lines: list[str] = []
    for raw_line in output.splitlines():
        if raw_line.startswith("===SKILL:") and raw_line.endswith("==="):
            _append_skill_metadata(blocks, current_name, current_lines)
            current_name = raw_line.removeprefix("===SKILL:").removesuffix("===").strip()
            current_lines = []
            continue
        current_lines.append(raw_line)
    _append_skill_metadata(blocks, current_name, current_lines)
    return blocks


def _append_skill_metadata(blocks: list[str], name: str, lines: list[str]) -> None:
    if not name or not lines:
        return
    front_matter = _front_matter_from_lines(lines)
    if front_matter is None:
        return
    blocks.append(f"## {name}\n\n```yaml\n{front_matter}\n```")


def _front_matter_from_lines(lines: list[str]) -> str | None:
    if not lines or lines[0] != "---":
        return None
    for index, line in enumerate(lines[1:], start=1):
        if line == "---":
            return "\n".join(lines[:index + 1]).strip()
    return None


def _decode_ws_token(token: str) -> AuthUser | None:
    try:
        return decode_access_token(token)
    except Exception:
        return None
