"""Persistent background execution for delegated subagent tasks."""

from __future__ import annotations

import asyncio
from collections import defaultdict
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from agents import Agent, RunContextWrapper, Runner, Tool, function_tool

from config import get_config
from core.runtime import put_nowait_drop_oldest
from core.runtime.context import AgentRuntimeContext, subagent_instance_id
from core.delegation.notifications import notification_prompt
from core.runtime.notification_dispatch import (
    dispatch_target_notifications,
    forget_target_notifications,
    target_notification_version,
    wait_for_target_notifications,
)
from core.runtime.input_items import build_user_message_item, text_input_content
from core.runtime.live_projection import LiveEventProjection
from core.runtime.partial_context import DeltaBuffer, flush_partial_context, track_delta
from core.runtime.streaming import StreamIdleTimeout, iter_normalized_stream_events
from core.sandbox.command_jobs import cancel_agent_async_sandbox_commands
from core.conversation.store import Z3r0Session
from database import get_engine
from logger import get_logger
from schema.agent.events import (
    AgentEventSchema,
    AgentInputPart,
    DoneEvent,
    ErrorEvent,
    RunStateEvent,
    SubagentTaskEvent,
    TextCompleteEvent,
    ThinkingCompleteEvent,
    ToolCallEvent,
    ToolResultEvent,
)
from schema.agent.subordinates import (
    AgentSubordinateStatus,
    AgentSubordinateTaskSnapshot,
    AgentSubordinateTaskToolItem,
    AgentSubordinateTaskToolResult,
)
from service.agent import notifications as agent_notifications
from service.agent.subordinates import TERMINAL_SUBAGENT_STATUSES as _TERMINAL_SUBAGENT_STATUSES
from service.agent import subordinates as agent_subordinates
from service.sandbox import async_jobs as sandbox_async_jobs


logger = get_logger(__name__)
@dataclass
class _SubagentJob:
    task: asyncio.Task[None]
    session_id: str
    parent_agent_instance_id: str
    sandbox_container_id: int | None


_jobs: dict[str, _SubagentJob] = {}
_session_starters: dict[str, set[asyncio.Task[AgentSubordinateTaskSnapshot]]] = defaultdict(set)
_subscribers: dict[str, set[asyncio.Queue[AgentEventSchema]]] = defaultdict(set)
_live_projections: dict[str, LiveEventProjection] = {}
_jobs_lock = asyncio.Lock()
_subscribers_lock = asyncio.Lock()

_SUBSCRIBER_QUEUE_SIZE = 256
_NOTIFICATION_POLL_SECONDS = 1.0


class _SubagentNotificationReady(Exception):
    """Raised internally to preempt a subagent turn when its notification is ready."""


_CANCEL_MESSAGE = "Subagent task canceled."


def build_subagent_tools(
    parent_code: str,
    mounted_codes: Iterable[str],
    *,
    get_child_agent: Callable[[str], Agent],
    get_code_to_name: Callable[[], dict[str, str]],
) -> list[Tool]:
    allowed = {code: code for code in mounted_codes}
    allowed_codes = ", ".join(sorted(allowed))

    async def start_subagent_task(ctx: RunContextWrapper[AgentRuntimeContext], agent_code: str, brief: str) -> str:
        """Start a configured subagent task in the background.

        Args:
            agent_code: str code of the configured subagent to run.
            brief: str self-contained task brief for the subagent.
                In WorkProject sessions, include the relevant task_id/task_title and instruct the subagent
                to update its WorkProject summary immediately after findings, useful negative results,
                blockers, evidence, decisions, or progress changes, before continuing to more tools when practical.

        Returns:
            JSON status including run_id, agent_code, status, timestamps, and automatic completion resume guidance.
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
        await _track_subagent_starter(ctx.context.session_id, starter)
        try:
            snapshot = await asyncio.shield(starter)
        except asyncio.CancelledError:
            starter.add_done_callback(_log_subagent_start_result)
            raise
        return _tool_response(
            task=snapshot,
            message=(
                "subagent task started; end this turn now. The task will resume automatically when "
                "the subagent finishes. Use read/list/cancel only if the user later asks for progress, "
                "task history, or cancellation."
            ),
        )

    async def read_subagent_task(ctx: RunContextWrapper[AgentRuntimeContext], run_id: str) -> str:
        """Read the latest state of a subagent task in the current session.

        Args:
            run_id: str subagent run id returned by start_subagent_task or list_subagent_tasks.

        Returns:
            JSON status with the task status, progress, result, error, and timestamps when available.
        """
        snapshot = await _resolve_task(ctx, run_id)
        if snapshot is None:
            return _tool_response(message="subagent task not found")
        return _tool_response(task=snapshot)

    async def list_subagent_tasks(ctx: RunContextWrapper[AgentRuntimeContext], limit: int = 20) -> str:
        """List recent subagent tasks visible to the current session user.

        Args:
            limit: int maximum number of recent subagent tasks to return.

        Returns:
            JSON status with recent task snapshots including run id, agent code, status, progress, and timestamps.
        """
        tasks = await agent_subordinates.list_subagent_tasks(
            session_id=ctx.context.session_id,
            user_id=ctx.context.user.id,
            user_role=ctx.context.user.role,
            limit=limit,
        )
        return _tool_response(tasks=tasks)

    async def cancel_subagent_task(ctx: RunContextWrapper[AgentRuntimeContext], run_id: str) -> str:
        """Request cancellation for a running subagent task in the current session.

        Args:
            run_id: str subagent run id returned by start_subagent_task or list_subagent_tasks.

        Returns:
            JSON status with the latest task state after cancellation is requested.
        """
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
                "Start a configured subagent task. Args: agent_code is one of "
                f"{allowed_codes}; brief is a self-contained task brief. "
                "Returns a persistent run id and resumes this agent automatically after the subagent finishes. "
                "For WorkProject tasks, include task_id/task_title and require summary/progress updates after "
                "findings, evidence, blockers, decisions, or progress changes."
            ),
        ),
        function_tool(
            read_subagent_task,
            name_override="read_subagent_task",
            description_override=(
                "Read the latest state for a subagent task. Args: run_id is the persistent subagent run id. "
                "Returns status, progress, result, error, and timestamps when available."
            ),
        ),
        function_tool(
            list_subagent_tasks,
            name_override="list_subagent_tasks",
            description_override=(
                "List recent subagent tasks visible in the current session. Args: limit is the maximum number "
                "of tasks to return. Returns task snapshots with run id, agent code, status, progress, and timestamps."
            ),
        ),
        function_tool(
            cancel_subagent_task,
            name_override="cancel_subagent_task",
            description_override=(
                "Request cancellation for a subagent task. Args: run_id is the persistent subagent run id. "
                "Returns the latest task state after cancellation is requested."
            ),
        ),
    ]
    return tools


async def _resolve_task(ctx: RunContextWrapper[AgentRuntimeContext], run_id: str) -> AgentSubordinateTaskSnapshot | None:
    return await agent_subordinates.get_subagent_task(
        run_id=run_id.strip(),
        session_id=ctx.context.session_id,
        user_id=ctx.context.user.id,
        user_role=ctx.context.user.role,
    )


def _tool_response(
    task: AgentSubordinateTaskSnapshot | None = None,
    tasks: list[AgentSubordinateTaskSnapshot] | None = None,
    message: str = "",
) -> str:
    return AgentSubordinateTaskToolResult(
        task=_task_tool_item(task),
        tasks=[_task_tool_item(item) for item in tasks or []],
        message=message,
    ).model_dump_json(
        exclude_none=True,
        exclude_defaults=True,
    )


def _task_tool_item(snapshot: AgentSubordinateTaskSnapshot | None) -> AgentSubordinateTaskToolItem | None:
    if snapshot is None:
        return None
    return AgentSubordinateTaskToolItem(
        run_id=snapshot.run_id,
        agent_code=snapshot.agent_code,
        agent_name=snapshot.agent_name,
        status=snapshot.status,
        result=snapshot.result,
        error=snapshot.error,
        progress=snapshot.progress,
    )


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
    snapshot = await agent_subordinates.create_subagent_task(
        session_id=context.session_id,
        parent_agent_code=parent_agent_code,
        parent_agent_instance_id=context.agent_instance_id,
        agent_code=agent_code,
        agent_name=code_to_name.get(agent_code, child_agent.name),
        brief=brief,
        nested_call_id=nested_call_id,
        owner_id=context.user.id,
    )
    await _mark_parent_session_running(snapshot, context)
    runtime_context = _subagent_context(context, snapshot, agent_code)
    async with _jobs_lock:
        task = asyncio.create_task(
            _run_subagent_task(
                snapshot=snapshot,
                child_agent=child_agent,
                code_to_name=code_to_name,
                context=runtime_context,
            ),
            name=f"subagent-{agent_code}-{snapshot.run_id}",
        )
        _jobs[snapshot.run_id] = _SubagentJob(
            task=task,
            session_id=snapshot.session_id,
            parent_agent_instance_id=snapshot.parent_agent_instance_id,
            sandbox_container_id=runtime_context.sandbox_container_id,
        )
    await _publish_task_snapshot(snapshot)
    logger.info("subagent task scheduled: %s agent=%s", snapshot.run_id, agent_code)
    return snapshot


async def cancel_subagent_task_run(snapshot: AgentSubordinateTaskSnapshot) -> AgentSubordinateTaskSnapshot:
    agent_instance_id = subagent_instance_id(snapshot.run_id)
    async with _jobs_lock:
        job = _jobs.get(snapshot.run_id)
    publish_now = job is None or job.task.done()
    if job is not None and not job.task.done():
        job.task.cancel()
    await cancel_agent_async_sandbox_commands(
        session_id=snapshot.session_id,
        agent_instance_id=agent_instance_id,
    )
    await agent_notifications.cancel_session_notifications(
        snapshot.session_id,
        _CANCEL_MESSAGE,
        target_agent_instance_id=agent_instance_id,
    )
    await _cancel_child_subagent_runs(snapshot.session_id, agent_instance_id, _CANCEL_MESSAGE)
    latest = await agent_subordinates.cancel_subagent_task_record(snapshot.run_id, _CANCEL_MESSAGE)
    snapshot = latest or snapshot
    if publish_now:
        await _publish_task_snapshot(snapshot)
    return snapshot


async def cancel_sandbox_subagent_runs(container_id: int) -> bool:
    async with _jobs_lock:
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
    starter_tasks = await _cancel_session_starters(session_id)
    job_tasks = await _cancel_session_jobs(session_id)
    if starter_tasks:
        await asyncio.gather(*starter_tasks, return_exceptions=True)

    job_tasks.extend(await _cancel_session_jobs(session_id))
    if job_tasks:
        await asyncio.gather(*job_tasks, return_exceptions=True)

    snapshots = await agent_subordinates.cancel_running_subagent_tasks_for_session(
        session_id,
        _CANCEL_MESSAGE,
    )
    for snapshot in snapshots:
        await _publish_task_snapshot(snapshot)

    return bool(starter_tasks or job_tasks or snapshots)


async def _track_subagent_starter(session_id: str, task: asyncio.Task[AgentSubordinateTaskSnapshot]) -> None:
    async with _jobs_lock:
        _session_starters[session_id].add(task)

    def _forget_starter(completed: asyncio.Task[AgentSubordinateTaskSnapshot]) -> None:
        starters = _session_starters.get(session_id)
        if starters is None:
            return
        starters.discard(completed)
        if not starters:
            _session_starters.pop(session_id, None)

    task.add_done_callback(_forget_starter)


async def _cancel_session_starters(session_id: str) -> list[asyncio.Task[AgentSubordinateTaskSnapshot]]:
    async with _jobs_lock:
        starters = list(_session_starters.pop(session_id, ()))
    pending = [task for task in starters if not task.done()]
    for task in pending:
        task.cancel()
    return pending


async def _cancel_session_jobs(session_id: str) -> list[asyncio.Task[None]]:
    async with _jobs_lock:
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
    await agent_notifications.reset_processing_notifications_all()
    stale_snapshots = await agent_subordinates.mark_stale_running_subagent_tasks_failed()
    for snapshot in stale_snapshots:
        await agent_notifications.cancel_session_notifications(
            snapshot.session_id,
            snapshot.error,
            target_agent_instance_id=subagent_instance_id(snapshot.run_id),
        )
        await _queue_parent_notification(snapshot)


async def stop_subagent_runtime() -> None:
    async with _jobs_lock:
        starter_tasks = [task for tasks in _session_starters.values() for task in tasks if not task.done()]
        _session_starters.clear()
        jobs = list(_jobs.values())
        _jobs.clear()
    for task in starter_tasks:
        task.cancel()
    for job in jobs:
        if not job.task.done():
            job.task.cancel()
    await asyncio.gather(
        *starter_tasks,
        *(job.task for job in jobs),
        return_exceptions=True,
    )

    snapshots = await agent_subordinates.cancel_running_subagent_tasks(_CANCEL_MESSAGE)
    for snapshot in snapshots:
        await _publish_task_snapshot(snapshot)


async def subscribe_session_events(
    session_id: str,
    include: Callable[[AgentEventSchema], bool] | None = None,
) -> asyncio.Queue[AgentEventSchema]:
    queue: asyncio.Queue[AgentEventSchema] = asyncio.Queue(maxsize=_SUBSCRIBER_QUEUE_SIZE)
    async with _subscribers_lock:
        projection = _live_projections.get(session_id)
        if projection is not None:
            for event in projection.snapshot(include):
                put_nowait_drop_oldest(queue, event)
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
        projection = _live_projection(session_id)
        projection.apply(event)
        if _is_nested_turn_done(event):
            projection.clear()
        targets = list(_subscribers.get(session_id, ()))
    for queue in targets:
        put_nowait_drop_oldest(queue, event)


async def _publish_task_snapshot(snapshot: AgentSubordinateTaskSnapshot) -> None:
    await publish_subagent_event(snapshot.session_id, _task_event(snapshot))
    if snapshot.status in _TERMINAL_SUBAGENT_STATUSES:
        await _clear_subagent_projection_if_idle(snapshot.session_id)


async def _clear_subagent_projection_if_idle(session_id: str) -> None:
    async with _jobs_lock:
        has_running = any(
            job.session_id == session_id and not job.task.done()
            for job in _jobs.values()
        )
    if has_running:
        return
    async with _subscribers_lock:
        _live_projections.pop(session_id, None)


def _live_projection(session_id: str) -> LiveEventProjection:
    projection = _live_projections.get(session_id)
    if projection is None:
        projection = LiveEventProjection()
        _live_projections[session_id] = projection
    return projection


def _is_nested_turn_done(event: AgentEventSchema) -> bool:
    return isinstance(event, DoneEvent) and bool(event.nested_call_id)


async def _run_subagent_task(
    *,
    snapshot: AgentSubordinateTaskSnapshot,
    child_agent: Agent,
    code_to_name: dict[str, str],
    context: AgentRuntimeContext,
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
    buffers: dict[str, DeltaBuffer] = {}
    try:
        max_turns = get_config().agent_runtime.subordinate_max_turns
        agent_config = get_config().agents.get(snapshot.agent_code)
        if agent_config is not None:
            brief_content = text_input_content(snapshot.brief)
            await memory_session.compact_if_needed(
                agent_config=agent_config,
                incoming_items=[build_user_message_item(brief_content)],
            )
        result = await _run_subagent_until_idle(
            initial_content=text_input_content(snapshot.brief),
            snapshot=snapshot,
            child_agent=child_agent,
            memory_session=memory_session,
            context=context,
            max_turns=max_turns,
            buffers=buffers,
        )
        completed = await agent_subordinates.complete_subagent_task(snapshot.run_id, _final_text(result))
        if completed is not None:
            await _queue_parent_notification(completed, context)
            await _publish_task_snapshot(completed)
    except asyncio.CancelledError:
        await cancel_agent_async_sandbox_commands(
            session_id=snapshot.session_id,
            agent_instance_id=context.agent_instance_id,
        )
        await agent_notifications.cancel_session_notifications(
            snapshot.session_id,
            _CANCEL_MESSAGE,
            target_agent_instance_id=context.agent_instance_id,
        )
        await _cancel_child_subagent_runs(snapshot.session_id, context.agent_instance_id, _CANCEL_MESSAGE)
        canceled = await agent_subordinates.cancel_subagent_task_record(snapshot.run_id, _CANCEL_MESSAGE)
        if canceled is not None:
            await _queue_parent_notification(canceled, context)
            await _publish_task_snapshot(canceled)
    except Exception as exc:
        logger.exception("subagent task failed: %s", snapshot.run_id)
        tagged_error = _tag_nested(ErrorEvent(created_at=datetime.now(), agent_name=child_agent.name, message=f"Subagent failed: {exc}"), snapshot)
        await publish_subagent_event(snapshot.session_id, tagged_error)
        await agent_notifications.cancel_session_notifications(
            snapshot.session_id,
            str(exc) or "subagent failed",
            target_agent_instance_id=context.agent_instance_id,
        )
        await _cancel_child_subagent_runs(snapshot.session_id, context.agent_instance_id, str(exc) or "subagent failed")
        failed = await agent_subordinates.fail_subagent_task(snapshot.run_id, str(exc) or "subagent failed")
        if failed is not None:
            await _queue_parent_notification(failed, context)
            await _publish_task_snapshot(failed)
    finally:
        await cancel_agent_async_sandbox_commands(
            session_id=snapshot.session_id,
            agent_instance_id=context.agent_instance_id,
        )
        await forget_target_notifications(context.agent_instance_id)
        async with _jobs_lock:
            current = _jobs.get(snapshot.run_id)
            if current is not None and current.task is asyncio.current_task():
                _jobs.pop(snapshot.run_id, None)
            has_running = any(
                job.session_id == snapshot.session_id and not job.task.done()
                for job in _jobs.values()
            )
        if not has_running:
            async with _subscribers_lock:
                _live_projections.pop(snapshot.session_id, None)
            await _mark_parent_session_stopped(snapshot.session_id)
            await _publish_parent_idle_if_inactive(snapshot.session_id)


async def _run_subagent_turn(
    *,
    content: list[AgentInputPart],
    snapshot: AgentSubordinateTaskSnapshot,
    child_agent: Agent,
    memory_session: Z3r0Session,
    context: AgentRuntimeContext,
    max_turns: int,
    buffers: dict[str, DeltaBuffer],
    preempt_on_notification: bool = True,
) -> Any:
    user_input = [build_user_message_item(content)]
    stream = Runner.run_streamed(
        starting_agent=child_agent,
        input=user_input,
        session=memory_session,
        context=context,
        max_turns=max_turns,
    )
    try:
        events = (
            _iter_subagent_events_until_notification(
                stream,
                session_id=snapshot.session_id,
                current_agent_name=child_agent.name,
                target_agent_instance_id=context.agent_instance_id,
            )
            if preempt_on_notification
            else iter_normalized_stream_events(stream, current_agent_name=child_agent.name)
        )
        async for event in events:
            track_delta(buffers, event)
            tagged = _tag_nested(event, snapshot)
            await publish_subagent_event(snapshot.session_id, tagged)
            await _update_progress_from_event(snapshot, event)
        buffers.clear()
    except _SubagentNotificationReady:
        await flush_partial_context(stream, memory_session, buffers, log_label="subagent")
        raise
    except asyncio.CancelledError:
        await flush_partial_context(stream, memory_session, buffers, log_label="subagent")
        raise
    except StreamIdleTimeout as exc:
        await flush_partial_context(stream, memory_session, buffers, log_label="subagent")
        raise RuntimeError(str(exc)) from exc
    except Exception:
        await flush_partial_context(stream, memory_session, buffers, log_label="subagent")
        raise
    return stream


async def _iter_subagent_events_until_notification(
    stream: Any,
    *,
    session_id: str,
    current_agent_name: str,
    target_agent_instance_id: str,
):
    events = iter_normalized_stream_events(stream, current_agent_name=current_agent_name)
    event_task = asyncio.create_task(anext(events), name=f"subagent-stream-next-{target_agent_instance_id}")
    if await agent_notifications.has_pending_notification(
        session_id=session_id,
        target_agent_instance_id=target_agent_instance_id,
    ):
        event_task.cancel()
        await asyncio.gather(event_task, return_exceptions=True)
        raise _SubagentNotificationReady
    notification_version = await target_notification_version(target_agent_instance_id)
    notification_task = asyncio.create_task(
        wait_for_target_notifications(
            target_agent_instance_id,
            after_version=notification_version,
            timeout_seconds=_NOTIFICATION_POLL_SECONDS,
        ),
        name=f"subagent-notification-watch-{target_agent_instance_id}",
    )
    try:
        while True:
            done, _ = await asyncio.wait(
                {event_task, notification_task},
                return_when=asyncio.FIRST_COMPLETED,
            )
            notification_ready = False
            if notification_task in done:
                notification_task.result()
                notification_ready = await agent_notifications.has_pending_notification(
                    session_id=session_id,
                    target_agent_instance_id=target_agent_instance_id,
                )
            if event_task in done:
                try:
                    event = event_task.result()
                except StopAsyncIteration:
                    return
                yield event
                event_task = asyncio.create_task(anext(events), name=f"subagent-stream-next-{target_agent_instance_id}")
            if notification_task in done:
                if notification_ready:
                    event_task.cancel()
                    await asyncio.gather(event_task, return_exceptions=True)
                    raise _SubagentNotificationReady
                notification_version = await target_notification_version(target_agent_instance_id)
                notification_task = asyncio.create_task(
                    wait_for_target_notifications(
                        target_agent_instance_id,
                        after_version=notification_version,
                        timeout_seconds=_NOTIFICATION_POLL_SECONDS,
                    ),
                    name=f"subagent-notification-watch-{target_agent_instance_id}",
                )
    finally:
        for task in (event_task, notification_task):
            if not task.done():
                task.cancel()
        await asyncio.gather(event_task, notification_task, return_exceptions=True)


async def _run_subagent_until_idle(
    *,
    initial_content: list[AgentInputPart],
    snapshot: AgentSubordinateTaskSnapshot,
    child_agent: Agent,
    memory_session: Z3r0Session,
    context: AgentRuntimeContext,
    max_turns: int,
    buffers: dict[str, DeltaBuffer],
) -> Any:
    result: Any = None
    try:
        result = await _run_subagent_turn(
            content=initial_content,
            snapshot=snapshot,
            child_agent=child_agent,
            memory_session=memory_session,
            context=context,
            max_turns=max_turns,
            buffers=buffers,
            preempt_on_notification=True,
        )
    except _SubagentNotificationReady:
        pass
    while True:
        notification = await agent_notifications.claim_next_pending_notification(
            session_id=snapshot.session_id,
            target_agent_instance_id=context.agent_instance_id,
        )
        if notification is None:
            version = await target_notification_version(context.agent_instance_id)
            if await agent_notifications.has_pending_notification(
                session_id=snapshot.session_id,
                target_agent_instance_id=context.agent_instance_id,
            ):
                continue
            if not await _subagent_has_running_background_work(snapshot.session_id, context.agent_instance_id):
                return result
            await wait_for_target_notifications(
                context.agent_instance_id,
                after_version=version,
                timeout_seconds=30,
            )
            continue
        try:
            result = await _run_subagent_turn(
                content=text_input_content(notification_prompt(notification)),
                snapshot=snapshot,
                child_agent=child_agent,
                memory_session=memory_session,
                context=context,
                max_turns=max_turns,
                buffers=buffers,
                preempt_on_notification=False,
            )
        except asyncio.CancelledError:
            await agent_notifications.release_notification(notification.id)
            raise
        except Exception as exc:
            await agent_notifications.fail_notification(notification.id, str(exc) or "subagent notification handling failed")
            raise
        else:
            await agent_notifications.complete_notification(notification.id)


async def _subagent_has_running_background_work(session_id: str, agent_instance_id: str) -> bool:
    if await sandbox_async_jobs.count_running_async_jobs_for_agent(
        session_id=session_id,
        agent_instance_id=agent_instance_id,
    ):
        return True
    return await agent_subordinates.has_running_child_subagent_tasks(
        session_id=session_id,
        parent_agent_instance_id=agent_instance_id,
    )


async def _cancel_child_subagent_runs(session_id: str, parent_agent_instance_id: str, error: str) -> None:
    async with _jobs_lock:
        tasks: list[asyncio.Task[None]] = []
        for run_id, job in list(_jobs.items()):
            if job.session_id != session_id or job.parent_agent_instance_id != parent_agent_instance_id:
                continue
            _jobs.pop(run_id, None)
            if not job.task.done():
                job.task.cancel()
                tasks.append(job.task)
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)

    snapshots = await agent_subordinates.cancel_running_child_subagent_tasks(
        session_id=session_id,
        parent_agent_instance_id=parent_agent_instance_id,
        error=error,
    )
    for snapshot in snapshots:
        await _publish_task_snapshot(snapshot)


async def _update_progress_from_event(snapshot: AgentSubordinateTaskSnapshot, event: AgentEventSchema) -> None:
    progress = _progress_from_event(event)
    if not progress:
        return
    latest = await agent_subordinates.update_subagent_progress(snapshot.run_id, progress)
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
        await agent_notifications.enqueue_subagent_finished_notification(
            snapshot,
            sandbox_container_id=context.sandbox_container_id if context else None,
            sandbox_container_generation=context.sandbox_container_generation if context else 0,
            sandbox_skill_metadata=context.sandbox_skill_metadata if context else (),
        )
        await _dispatch_parent_notification(snapshot, context)
    except Exception:
        logger.exception("failed to queue parent notification for subagent task: %s", snapshot.run_id)


async def _dispatch_parent_notification(
    snapshot: AgentSubordinateTaskSnapshot,
    context: AgentRuntimeContext | None,
) -> None:
    if context is None:
        return
    try:
        parent_context = AgentRuntimeContext(
            session_id=context.session_id,
            user=context.user,
            agent_code=snapshot.parent_agent_code,
            agent_instance_id=snapshot.parent_agent_instance_id,
            knowledge_generation=context.knowledge_generation,
            sandbox_container_id=context.sandbox_container_id,
            sandbox_container_generation=context.sandbox_container_generation,
            sandbox_skill_metadata=context.sandbox_skill_metadata,
            work_project_id=context.work_project_id,
        )
        await dispatch_target_notifications(
            parent_context,
            target_agent_instance_id=snapshot.parent_agent_instance_id,
        )
    except Exception:
        logger.exception("failed to schedule parent notification drain: %s", snapshot.run_id)


def _tag_nested(event: AgentEventSchema, snapshot: AgentSubordinateTaskSnapshot) -> AgentEventSchema:
    if not hasattr(event, "nested_for"):
        return event
    return event.model_copy(update={
        "nested_for": snapshot.parent_agent_code,
        "nested_call_id": snapshot.nested_call_id,
    })


async def _mark_parent_session_running(
    snapshot: AgentSubordinateTaskSnapshot,
    context: AgentRuntimeContext,
) -> None:
    try:
        from service.agent import sessions as agent_sessions

        await agent_sessions.mark_session_running(
            snapshot.session_id,
            agent_code=snapshot.parent_agent_code,
            sandbox_container_id=context.sandbox_container_id,
            sandbox_container_generation=context.sandbox_container_generation,
        )
    except Exception:
        logger.debug("failed to mark parent session running: %s", snapshot.session_id, exc_info=True)


async def _mark_parent_session_stopped(session_id: str) -> None:
    try:
        from service.agent import sessions as agent_sessions

        await agent_sessions.mark_session_stopped(session_id)
    except Exception:
        logger.debug("failed to mark parent session stopped: %s", session_id, exc_info=True)


async def _publish_parent_idle_if_inactive(session_id: str) -> None:
    try:
        from service.agent import sessions as agent_sessions

        if await agent_sessions.has_active_session_runtime(session_id):
            return
        await publish_subagent_event(
            session_id,
            RunStateEvent(created_at=datetime.now(), running=False),
        )
        async with _subscribers_lock:
            _live_projections.pop(session_id, None)
    except Exception:
        logger.debug("failed to publish parent session idle: %s", session_id, exc_info=True)


def _subagent_context(
    context: AgentRuntimeContext,
    snapshot: AgentSubordinateTaskSnapshot,
    agent_code: str,
) -> AgentRuntimeContext:
    return AgentRuntimeContext(
        session_id=context.session_id,
        user=context.user,
        agent_code=agent_code,
        agent_instance_id=subagent_instance_id(snapshot.run_id),
        nested_for_agent_code=snapshot.parent_agent_code,
        nested_call_id=snapshot.nested_call_id,
        knowledge_generation=context.knowledge_generation,
        sandbox_container_id=context.sandbox_container_id,
        sandbox_container_generation=context.sandbox_container_generation,
        sandbox_skill_metadata=context.sandbox_skill_metadata,
        work_project_id=context.work_project_id,
    )


def _final_text(result: Any) -> str:
    output = getattr(result, "final_output", None)
    if output is None:
        return ""
    return output if isinstance(output, str) else str(output)
