"""Persistent background execution for delegated subagent tasks."""

from __future__ import annotations

import asyncio
from collections import defaultdict
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from agents import Agent, RunContextWrapper, Runner, Tool, TResponseInputItem, function_tool
from agents.stream_events import AgentUpdatedStreamEvent

from config import get_config
from core.context import AgentRuntimeContext, subagent_instance_id
from core.events import event_from_sdk_stream
from core.jobs import cancel_agent_async_sandbox_commands, has_pending_async_sandbox_commands
from core.notifications import notification_prompt
from core.session import Z3r0Session
from database import get_engine
from logger import get_logger
from schema.agent_event_schema import (
    AgentEventSchema,
    ErrorEvent,
    SubagentTaskEvent,
    TextCompleteEvent,
    TextDeltaEvent,
    ThinkingCompleteEvent,
    ThinkingDeltaEvent,
    ToolCallEvent,
    ToolResultEvent,
)
from schema.agent_notification_schema import AgentNotificationSnapshot
from schema.agent_subordinate_schema import AgentSubordinateStatus, AgentSubordinateTaskSnapshot, AgentSubordinateTaskToolResponse
from service import agent_subordinate_service
from service import agent_notification_service


logger = get_logger(__name__)


@dataclass
class _DeltaBuffer:
    is_thinking: bool
    item_id: str
    content: str = ""
    complete: bool = False


@dataclass
class _SubagentJob:
    task: asyncio.Task[None]
    done: asyncio.Event
    session_id: str
    sandbox_container_id: int | None


_jobs: dict[str, _SubagentJob] = {}
_session_starters: dict[str, set[asyncio.Task[AgentSubordinateTaskSnapshot]]] = defaultdict(set)
_subscribers: dict[str, set[asyncio.Queue[AgentEventSchema]]] = defaultdict(set)
_subscribers_lock = asyncio.Lock()

_DELTA_TYPES: tuple[type, ...] = (TextDeltaEvent, ThinkingDeltaEvent)
_COMPLETE_TYPES: tuple[type, ...] = (TextCompleteEvent, ThinkingCompleteEvent)
_MAX_WAIT_TIMEOUT_SECONDS = 300
_SUBSCRIBER_QUEUE_SIZE = 256
_CANCEL_MESSAGE = "Subagent task canceled."


def build_subagent_tools(
    parent_code: str,
    mounted_codes: Iterable[str],
    *,
    get_child_agent: Callable[[str], Agent],
    get_code_to_name: Callable[[], dict[str, str]],
    allow_wait: bool = True,
) -> list[Tool]:
    allowed = {code: code for code in mounted_codes}
    allowed_codes = ", ".join(sorted(allowed))

    async def start_subagent_task(ctx: RunContextWrapper[AgentRuntimeContext], agent_code: str, brief: str) -> str:
        """Start a configured subagent task in the background.

        Args:
            agent_code: Code of the configured subagent to run.
            brief: Self-contained task brief for the subagent.

        Returns:
            JSON status including run_id, agent_code, status, and timestamps.
        """
        code = agent_code.strip()
        if code not in allowed:
            return _tool_response(message=f"unknown subagent '{code}'. allowed: {allowed_codes}")
        body = brief.strip()
        if not body:
            return _tool_response(message="brief is required")

        child_agent = get_child_agent(code)
        code_to_name = get_code_to_name()
        starter = asyncio.create_task(
            start_subagent_task_run(
                child_agent=child_agent,
                code_to_name=code_to_name,
                context=ctx.context,
                parent_agent_code=parent_code,
                agent_code=code,
                brief=body,
                nested_call_id=getattr(ctx, "tool_call_id", "") or "",
            ),
            name=f"subagent-starter-{code}",
        )
        _track_subagent_starter(ctx.context.session_id, starter)
        try:
            snapshot = await asyncio.shield(starter)
        except asyncio.CancelledError:
            starter.add_done_callback(_log_subagent_start_result)
            raise
        return _tool_response(
            task=snapshot,
            message=(
                "subagent task started; end this turn now and wait for the async completion notification "
                "before taking over again"
            ),
        )

    async def read_subagent_task(ctx: RunContextWrapper[AgentRuntimeContext], run_id: str) -> str:
        """Read current status/result/error/progress for a subagent task."""
        snapshot = await _resolve_task(ctx, run_id)
        if snapshot is None:
            return _tool_response(message="subagent task not found")
        return _tool_response(task=snapshot)

    async def list_subagent_tasks(ctx: RunContextWrapper[AgentRuntimeContext], limit: int = 20) -> str:
        """List recent subagent tasks for this session."""
        tasks = await agent_subordinate_service.list_subagent_tasks(
            session_id=ctx.context.session_id,
            user_id=ctx.context.user.id,
            user_role=ctx.context.user.role,
            limit=limit,
        )
        return AgentSubordinateTaskToolResponse(tasks=tasks).model_dump_json()

    async def wait_subagent_task(
        ctx: RunContextWrapper[AgentRuntimeContext], run_id: str, timeout_seconds: int = 30,
    ) -> str:
        """Wait briefly for a subagent task to finish, or return its current running status."""
        snapshot = await _resolve_task(ctx, run_id)
        if snapshot is None:
            return _tool_response(message="subagent task not found")
        latest = await wait_subagent_task_run(snapshot, timeout_seconds)
        return _tool_response(task=latest)

    async def cancel_subagent_task(ctx: RunContextWrapper[AgentRuntimeContext], run_id: str) -> str:
        """Cancel a running subagent task when practical."""
        snapshot = await _resolve_task(ctx, run_id)
        if snapshot is None:
            return _tool_response(message="subagent task not found")
        latest = await cancel_subagent_task_run(snapshot)
        return _tool_response(task=latest, message="subagent task cancel requested")

    tools = [
        function_tool(
            start_subagent_task,
            name_override="start_subagent_task",
            description_override=(
                "Start a configured subagent by code, return a persistent run id, then end the current turn. "
                f"Allowed agent_code values: {allowed_codes}."
            ),
        ),
        function_tool(
            read_subagent_task,
            name_override="read_subagent_task",
            description_override="Read current status, result, error, and progress for a persistent subagent run id.",
        ),
        function_tool(
            list_subagent_tasks,
            name_override="list_subagent_tasks",
            description_override="List recent persistent subagent tasks for this session, including run ids and current statuses.",
        ),
        function_tool(
            cancel_subagent_task,
            name_override="cancel_subagent_task",
            description_override="Cancel a persistent subagent run id when practical.",
        ),
    ]
    if allow_wait:
        tools.insert(
            1,
            function_tool(
                wait_subagent_task,
                name_override="wait_subagent_task",
                description_override=(
                    "Wait for a persistent subagent run id, returning result if done or current status if still running."
                ),
            ),
        )
    return tools


async def _resolve_task(ctx: RunContextWrapper[AgentRuntimeContext], run_id: str) -> AgentSubordinateTaskSnapshot | None:
    return await agent_subordinate_service.get_subagent_task(
        run_id=run_id.strip(),
        session_id=ctx.context.session_id,
        user_id=ctx.context.user.id,
        user_role=ctx.context.user.role,
    )


def _tool_response(task: AgentSubordinateTaskSnapshot | None = None, message: str = "") -> str:
    return AgentSubordinateTaskToolResponse(task=task, message=message).model_dump_json()


def _log_subagent_start_result(task: asyncio.Task[AgentSubordinateTaskSnapshot]) -> None:
    try:
        task.result()
    except asyncio.CancelledError:
        logger.warning("subagent task starter was canceled before scheduling completed")
    except Exception:
        logger.exception("subagent task starter failed after parent turn cancellation")


async def start_subagent_task_run(
    *,
    child_agent: Agent,
    code_to_name: dict[str, str],
    context: AgentRuntimeContext,
    parent_agent_code: str,
    agent_code: str,
    brief: str,
    nested_call_id: str,
) -> AgentSubordinateTaskSnapshot:
    snapshot = await agent_subordinate_service.create_subagent_task(
        session_id=context.session_id,
        parent_agent_code=parent_agent_code,
        parent_agent_instance_id=context.agent_instance_id,
        agent_code=agent_code,
        agent_name=code_to_name.get(agent_code, child_agent.name),
        brief=brief,
        nested_call_id=nested_call_id,
        owner_id=context.user.id,
    )
    runtime_context = _clone_context_for_background(context)
    _set_context_agent_code(runtime_context, agent_code)
    _set_context_agent_instance(runtime_context, subagent_instance_id(snapshot.run_id))
    _set_context_nested_scope(runtime_context, snapshot.parent_agent_code, snapshot.nested_call_id)
    done = asyncio.Event()
    task = asyncio.create_task(
        _run_subagent_task(
            snapshot=snapshot,
            child_agent=child_agent,
            code_to_name=code_to_name,
            context=runtime_context,
            done=done,
        ),
        name=f"subagent-{agent_code}-{snapshot.run_id}",
    )
    _jobs[snapshot.run_id] = _SubagentJob(
        task=task,
        done=done,
        session_id=snapshot.session_id,
        sandbox_container_id=runtime_context.sandbox_container_id,
    )
    await publish_subagent_event(snapshot.session_id, _task_event(snapshot))
    logger.info("subagent task scheduled: %s agent=%s", snapshot.run_id, agent_code)
    return snapshot


async def wait_subagent_task_run(snapshot: AgentSubordinateTaskSnapshot, timeout_seconds: int) -> AgentSubordinateTaskSnapshot:
    if snapshot.status in agent_subordinate_service.TERMINAL_SUBAGENT_STATUSES:
        return snapshot
    job = _jobs.get(snapshot.run_id)
    if job is None:
        return snapshot
    timeout = max(0, min(timeout_seconds, _MAX_WAIT_TIMEOUT_SECONDS))
    if timeout:
        try:
            await asyncio.wait_for(job.done.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            pass
    latest = await agent_subordinate_service.get_subagent_task_internal(snapshot.run_id)
    return latest or snapshot


async def cancel_subagent_task_run(snapshot: AgentSubordinateTaskSnapshot) -> AgentSubordinateTaskSnapshot:
    agent_instance_id = subagent_instance_id(snapshot.run_id)
    job = _jobs.get(snapshot.run_id)
    publish_now = job is None or job.task.done()
    if job is not None and not job.task.done():
        job.task.cancel()
    await cancel_agent_async_sandbox_commands(
        session_id=snapshot.session_id,
        agent_instance_id=agent_instance_id,
    )
    await agent_notification_service.cancel_session_notifications(
        snapshot.session_id,
        _CANCEL_MESSAGE,
        target_agent_instance_id=agent_instance_id,
    )
    latest = await agent_subordinate_service.cancel_subagent_task_record(snapshot.run_id, _CANCEL_MESSAGE)
    snapshot = latest or snapshot
    if publish_now:
        await publish_subagent_event(snapshot.session_id, _task_event(snapshot))
    return snapshot


async def cancel_sandbox_subagent_runs(container_id: int) -> bool:
    tasks = [
        _jobs.pop(run_id).task
        for run_id, job in list(_jobs.items())
        if job.sandbox_container_id == container_id
    ]
    if not tasks:
        return False
    for task in tasks:
        if not task.done():
            task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    return True


async def cancel_session_subagent_runs(session_id: str) -> bool:
    starter_tasks = _cancel_session_starters(session_id)
    job_tasks = _cancel_session_jobs(session_id)
    if starter_tasks:
        await asyncio.gather(*starter_tasks, return_exceptions=True)

    job_tasks.extend(_cancel_session_jobs(session_id))
    if job_tasks:
        await asyncio.gather(*job_tasks, return_exceptions=True)

    snapshots = await agent_subordinate_service.cancel_running_subagent_tasks_for_session(
        session_id,
        _CANCEL_MESSAGE,
    )
    for snapshot in snapshots:
        await publish_subagent_event(snapshot.session_id, _task_event(snapshot))

    return bool(starter_tasks or job_tasks or snapshots)


def _track_subagent_starter(session_id: str, task: asyncio.Task[AgentSubordinateTaskSnapshot]) -> None:
    _session_starters[session_id].add(task)

    def _forget_starter(completed: asyncio.Task[AgentSubordinateTaskSnapshot]) -> None:
        starters = _session_starters.get(session_id)
        if starters is None:
            return
        starters.discard(completed)
        if not starters:
            _session_starters.pop(session_id, None)

    task.add_done_callback(_forget_starter)


def _cancel_session_starters(session_id: str) -> list[asyncio.Task[AgentSubordinateTaskSnapshot]]:
    starters = list(_session_starters.pop(session_id, ()))
    pending = [task for task in starters if not task.done()]
    for task in pending:
        task.cancel()
    return pending


def _cancel_session_jobs(session_id: str) -> list[asyncio.Task[None]]:
    tasks: list[asyncio.Task[None]] = []
    for run_id, job in list(_jobs.items()):
        if job.session_id != session_id:
            continue
        _jobs.pop(run_id, None)
        if not job.task.done():
            job.task.cancel()
            tasks.append(job.task)
    return tasks


async def start_subagent_runtime() -> None:
    await agent_notification_service.reset_processing_notifications_all()
    stale_snapshots = await agent_subordinate_service.mark_stale_running_subagent_tasks_failed()
    for snapshot in stale_snapshots:
        await agent_notification_service.cancel_session_notifications(
            snapshot.session_id,
            snapshot.error,
            target_agent_instance_id=subagent_instance_id(snapshot.run_id),
        )
        await _queue_parent_notification(snapshot)


async def stop_subagent_runtime() -> None:
    starter_tasks = [task for tasks in _session_starters.values() for task in tasks if not task.done()]
    _session_starters.clear()
    for task in starter_tasks:
        task.cancel()

    jobs = list(_jobs.values())
    _jobs.clear()
    for job in jobs:
        if not job.task.done():
            job.task.cancel()
    await asyncio.gather(
        *starter_tasks,
        *(job.task for job in jobs),
        return_exceptions=True,
    )

    snapshots = await agent_subordinate_service.cancel_running_subagent_tasks(_CANCEL_MESSAGE)
    for snapshot in snapshots:
        await publish_subagent_event(snapshot.session_id, _task_event(snapshot))


async def subscribe_session_events(session_id: str) -> asyncio.Queue[AgentEventSchema]:
    queue: asyncio.Queue[AgentEventSchema] = asyncio.Queue(maxsize=_SUBSCRIBER_QUEUE_SIZE)
    async with _subscribers_lock:
        _subscribers[session_id].add(queue)
    return queue


async def unsubscribe_session_events(session_id: str, queue: asyncio.Queue[AgentEventSchema]) -> None:
    async with _subscribers_lock:
        queues = _subscribers.get(session_id)
        if queues is None:
            return
        queues.discard(queue)
        if not queues:
            _subscribers.pop(session_id, None)


async def publish_subagent_event(session_id: str, event: AgentEventSchema) -> None:
    if not session_id:
        return
    async with _subscribers_lock:
        targets = list(_subscribers.get(session_id, ()))
    for queue in targets:
        try:
            queue.put_nowait(event)
        except asyncio.QueueFull:
            logger.debug("subagent event dropped for slow subscriber session=%s", session_id)


async def _run_subagent_task(
    *,
    snapshot: AgentSubordinateTaskSnapshot,
    child_agent: Agent,
    code_to_name: dict[str, str],
    context: AgentRuntimeContext,
    done: asyncio.Event,
) -> None:
    memory_session = Z3r0Session(
        session_id=snapshot.session_id,
        engine=get_engine(),
        viewing_agent_code=snapshot.agent_code,
        agent_code_to_name=code_to_name,
        nested_for_agent_code=snapshot.parent_agent_code,
        nested_call_id=snapshot.nested_call_id,
    )
    result: Any = None
    buffers: dict[str, _DeltaBuffer] = {}
    try:
        max_turns = get_config().agent_runtime.subordinate_max_turns
        agent_config = get_config().agents.get(snapshot.agent_code)
        if agent_config is not None:
            await memory_session.compact_if_needed(
                agent_config=agent_config,
                incoming_items=[{"type": "message", "role": "user", "content": snapshot.brief}],
            )
        result = await _run_subagent_turn(
            prompt=snapshot.brief,
            snapshot=snapshot,
            child_agent=child_agent,
            memory_session=memory_session,
            context=context,
            max_turns=max_turns,
            buffers=buffers,
        )
        while True:
            notification = await _wait_subagent_notification(snapshot, context)
            if notification is None:
                break
            notification_buffers: dict[str, _DeltaBuffer] = {}
            notification_result: Any = None
            try:
                notification_result = await _run_subagent_turn(
                    prompt=notification_prompt(notification),
                    snapshot=snapshot,
                    child_agent=child_agent,
                    memory_session=memory_session,
                    context=context,
                    max_turns=max_turns,
                    buffers=notification_buffers,
                )
                result = notification_result
            except asyncio.CancelledError:
                await agent_notification_service.release_notification(notification.id)
                raise
            except Exception as exc:
                await agent_notification_service.fail_notification(
                    notification.id,
                    str(exc) or "subagent notification handling failed",
                )
                raise
            else:
                await agent_notification_service.complete_notification(notification.id)

        completed = await agent_subordinate_service.complete_subagent_task(snapshot.run_id, _final_text(result))
        if completed is not None:
            await _queue_parent_notification(completed, context)
            await publish_subagent_event(completed.session_id, _task_event(completed))
    except asyncio.CancelledError:
        await cancel_agent_async_sandbox_commands(
            session_id=snapshot.session_id,
            agent_instance_id=context.agent_instance_id,
        )
        await agent_notification_service.cancel_session_notifications(
            snapshot.session_id,
            _CANCEL_MESSAGE,
            target_agent_instance_id=context.agent_instance_id,
        )
        canceled = await agent_subordinate_service.cancel_subagent_task_record(snapshot.run_id, _CANCEL_MESSAGE)
        if canceled is not None:
            await _queue_parent_notification(canceled, context)
            await publish_subagent_event(canceled.session_id, _task_event(canceled))
    except Exception as exc:
        logger.exception("subagent task failed: %s", snapshot.run_id)
        tagged_error = _tag_nested(ErrorEvent(created_at=datetime.now(), agent_name=child_agent.name, message=f"Subagent failed: {exc}"), snapshot)
        await publish_subagent_event(snapshot.session_id, tagged_error)
        await agent_notification_service.cancel_session_notifications(
            snapshot.session_id,
            str(exc) or "subagent failed",
            target_agent_instance_id=context.agent_instance_id,
        )
        failed = await agent_subordinate_service.fail_subagent_task(snapshot.run_id, str(exc) or "subagent failed")
        if failed is not None:
            await _queue_parent_notification(failed, context)
            await publish_subagent_event(failed.session_id, _task_event(failed))
    finally:
        done.set()
        current = _jobs.get(snapshot.run_id)
        if current is not None and current.done is done:
            _jobs.pop(snapshot.run_id, None)


async def _run_subagent_turn(
    *,
    prompt: str,
    snapshot: AgentSubordinateTaskSnapshot,
    child_agent: Agent,
    memory_session: Z3r0Session,
    context: AgentRuntimeContext,
    max_turns: int,
    buffers: dict[str, _DeltaBuffer],
) -> Any:
    stream = Runner.run_streamed(
        starting_agent=child_agent,
        input=prompt,
        session=memory_session,
        context=context,
        max_turns=max_turns,
    )
    try:
        async for sdk_event in stream.stream_events():
            if isinstance(sdk_event, AgentUpdatedStreamEvent):
                continue
            event = event_from_sdk_stream(sdk_event, child_agent.name)
            if event is None:
                continue
            _track_delta(buffers, event)
            tagged = _tag_nested(event, snapshot)
            await publish_subagent_event(snapshot.session_id, tagged)
            await _update_progress_from_event(snapshot, event)
    except asyncio.CancelledError:
        await _flush_partial_context(stream, memory_session, buffers)
        raise
    except Exception:
        await _flush_partial_context(stream, memory_session, buffers)
        raise
    return stream


async def _wait_subagent_notification(
    snapshot: AgentSubordinateTaskSnapshot,
    context: AgentRuntimeContext,
) -> AgentNotificationSnapshot | None:
    target_agent_instance_id = context.agent_instance_id
    while True:
        notification = await agent_notification_service.claim_next_pending_notification(
            session_id=snapshot.session_id,
            target_agent_instance_id=target_agent_instance_id,
        )
        if notification is not None:
            return notification
        if has_pending_async_sandbox_commands(
            session_id=snapshot.session_id,
            agent_instance_id=target_agent_instance_id,
        ):
            await asyncio.sleep(0.5)
            continue
        return None


async def _update_progress_from_event(snapshot: AgentSubordinateTaskSnapshot, event: AgentEventSchema) -> None:
    progress = _progress_from_event(event)
    if not progress:
        return
    latest = await agent_subordinate_service.update_subagent_progress(snapshot.run_id, progress)
    if latest is not None:
        await publish_subagent_event(latest.session_id, _task_event(latest))


def _progress_from_event(event: AgentEventSchema) -> str:
    if isinstance(event, ToolCallEvent):
        return f"calling tool: {event.name or event.call_id}"
    if isinstance(event, ToolResultEvent):
        return "tool completed"
    if isinstance(event, TextCompleteEvent):
        return "reported output"
    if isinstance(event, ThinkingCompleteEvent):
        return "completed reasoning"
    return ""


def _task_event(snapshot: AgentSubordinateTaskSnapshot) -> AgentEventSchema:
    return SubagentTaskEvent(
        created_at=snapshot.updated_at,
        agent_name=snapshot.agent_name,
        nested_for=snapshot.parent_agent_code,
        nested_call_id=snapshot.nested_call_id,
        run_id=snapshot.run_id,
        parent_agent_code=snapshot.parent_agent_code,
        parent_agent_instance_id=snapshot.parent_agent_instance_id,
        agent_code=snapshot.agent_code,
        status=snapshot.status,
        result=snapshot.result,
        error=snapshot.error,
        progress=snapshot.progress,
    )


async def _queue_parent_notification(
    snapshot: AgentSubordinateTaskSnapshot,
    context: AgentRuntimeContext | None = None,
) -> None:
    if not snapshot.parent_agent_code:
        return
    if snapshot.status == AgentSubordinateStatus.CANCELED:
        return
    try:
        await agent_notification_service.enqueue_subagent_finished_notification(
            snapshot,
            sandbox_container_id=context.sandbox_container_id if context else None,
            sandbox_container_generation=context.sandbox_container_generation if context else 0,
            sandbox_skill_metadata=context.sandbox_skill_metadata if context else (),
        )
    except Exception:
        logger.exception("failed to queue parent notification for subagent task: %s", snapshot.run_id)


def _tag_nested(event: AgentEventSchema, snapshot: AgentSubordinateTaskSnapshot) -> AgentEventSchema:
    if not hasattr(event, "nested_for"):
        return event
    return event.model_copy(update={
        "nested_for": snapshot.parent_agent_code,
        "nested_call_id": snapshot.nested_call_id,
    })


def _clone_context_for_background(
    context: AgentRuntimeContext,
) -> AgentRuntimeContext | RunContextWrapper[AgentRuntimeContext]:
    cloned = AgentRuntimeContext(
        session_id=context.session_id,
        user=context.user,
        agent_code=context.agent_code,
        agent_instance_id=context.agent_instance_id,
        nested_for_agent_code=context.nested_for_agent_code,
        nested_call_id=context.nested_call_id,
        knowledge_generation=context.knowledge_generation,
        sandbox_container_id=context.sandbox_container_id,
        sandbox_container_generation=context.sandbox_container_generation,
        sandbox_skill_metadata=context.sandbox_skill_metadata,
    )
    return RunContextWrapper(context=cloned) if isinstance(context, RunContextWrapper) else cloned


def _set_context_agent_code(
    context: AgentRuntimeContext | RunContextWrapper[AgentRuntimeContext],
    agent_code: str,
) -> None:
    target = context.context if isinstance(context, RunContextWrapper) else context
    target.agent_code = agent_code


def _set_context_agent_instance(
    context: AgentRuntimeContext | RunContextWrapper[AgentRuntimeContext],
    agent_instance_id: str,
) -> None:
    target = context.context if isinstance(context, RunContextWrapper) else context
    target.agent_instance_id = agent_instance_id


def _set_context_nested_scope(
    context: AgentRuntimeContext | RunContextWrapper[AgentRuntimeContext],
    nested_for_agent_code: str,
    nested_call_id: str,
) -> None:
    target = context.context if isinstance(context, RunContextWrapper) else context
    target.nested_for_agent_code = nested_for_agent_code
    target.nested_call_id = nested_call_id


def _track_delta(buffers: dict[str, _DeltaBuffer], event: AgentEventSchema) -> None:
    if isinstance(event, _DELTA_TYPES):
        buf = buffers.get(event.item_id)
        if buf is None:
            buf = _DeltaBuffer(is_thinking=isinstance(event, ThinkingDeltaEvent), item_id=event.item_id)
            buffers[event.item_id] = buf
        buf.content += event.delta
    elif isinstance(event, _COMPLETE_TYPES):
        buf = buffers.get(event.item_id)
        if buf is None:
            buf = _DeltaBuffer(is_thinking=isinstance(event, ThinkingCompleteEvent), item_id=event.item_id)
            buffers[event.item_id] = buf
        buf.content = event.text
        buf.complete = True


async def _flush_partial_context(
    result: Any, memory_session: Z3r0Session, buffers: dict[str, _DeltaBuffer],
) -> None:
    if result is None or getattr(result, "is_complete", True):
        return
    try:
        result.cancel(mode="immediate")
    except Exception:
        logger.warning("failed to cancel subagent SDK stream", exc_info=True)
    items: list[TResponseInputItem] = [
        _partial_reasoning_item(buf) if buf.is_thinking else _partial_assistant_item(buf)
        for buf in buffers.values() if buf.content
    ]
    if not items:
        return
    try:
        await memory_session.add_items(items)
    except Exception:
        logger.warning("failed to inject partial subagent context", exc_info=True)


def _partial_assistant_item(buf: _DeltaBuffer) -> TResponseInputItem:
    return {
        "id": f"partial_{buf.item_id}",
        "type": "message",
        "role": "assistant",
        "content": [{"type": "output_text", "text": buf.content, "annotations": []}],
        "status": "completed" if buf.complete else "incomplete",
    }


def _partial_reasoning_item(buf: _DeltaBuffer) -> TResponseInputItem:
    return {
        "id": f"partial_{buf.item_id}",
        "type": "reasoning",
        "summary": [{"type": "summary_text", "text": buf.content}],
        "status": "completed" if buf.complete else "incomplete",
    }


def _final_text(result: Any) -> str:
    output = getattr(result, "final_output", None)
    if output is None:
        return ""
    return output if isinstance(output, str) else str(output)
