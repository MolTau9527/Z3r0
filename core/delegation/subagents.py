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
from core.conversation.context_budget import build_context_run_config
from core.runtime.context import AgentRuntimeContext, subagent_instance_id
from core.runtime.notification_dispatch import (
    forget_target_notifications,
    signal_target_notifications,
)
from core.runtime.input_items import build_user_message_item, text_input_content
from core.runtime.partial_context import DeltaBuffer, discard_partial_stream, incomplete_segment_events, track_delta
from core.runtime.streaming import StreamIdleTimeout, next_segment_scope
from core.task_runtime import InterruptSignal, TurnTrigger, iter_interruptible_events, run_until_idle
from core.sandbox.command_jobs import cancel_agent_async_sandbox_commands
from core import extract_message_text
from core.conversation.store import Z3r0Session, fetch_stored_items
from database import get_async_session, get_engine
from logger import get_logger
from schema.agent.events import (
    AgentEventSchema,
    DoneEvent,
    ErrorEvent,
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
_jobs_lock = asyncio.Lock()

_SUBAGENT_RESULT_MAX_CHARS = 20000

_CANCEL_MESSAGE = "Subagent task canceled."


def build_subagent_tools(
    parent_code: str,
    mounted_codes: Iterable[str],
    *,
    get_child_agent: Callable[[str], Agent],
    get_code_to_name: Callable[[], dict[str, str]],
) -> list[Tool]:
    allowed = frozenset(mounted_codes)
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
    early_jobs = await _cancel_session_jobs(session_id)
    if starter_tasks:
        await asyncio.gather(*starter_tasks, return_exceptions=True)
    late_jobs = await _cancel_session_jobs(session_id)
    all_jobs = early_jobs + late_jobs
    if all_jobs:
        await asyncio.gather(*all_jobs, return_exceptions=True)

    snapshots = await agent_subordinates.cancel_running_subagent_tasks_for_session(
        session_id,
        _CANCEL_MESSAGE,
    )
    for snapshot in snapshots:
        await _publish_task_snapshot(snapshot)

    return bool(starter_tasks or all_jobs or snapshots)


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


def _publish_event(session_id: str, event: AgentEventSchema) -> None:
    """Publish an event through the unified session event bus."""
    from core.runtime.session import get_agent_pool

    get_agent_pool().publish(session_id, event)


async def _publish_task_snapshot(snapshot: AgentSubordinateTaskSnapshot) -> None:
    _publish_event(snapshot.session_id, _task_event(snapshot))
    if snapshot.status in _TERMINAL_SUBAGENT_STATUSES:
        await _notify_session_idle(snapshot.session_id)


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
    max_turns = get_config().agent_runtime.subordinate_max_turns
    try:
        agent_config = get_config().agents.get(snapshot.agent_code)

        async def _run_turn(trigger: TurnTrigger) -> Any:
            user_input = [build_user_message_item(trigger.content)]
            if agent_config is not None:
                await memory_session.compact_if_needed(
                    agent_config=agent_config,
                    incoming_items=user_input,
                )
            stream = Runner.run_streamed(
                starting_agent=child_agent,
                input=user_input,
                session=memory_session,
                context=context,
                max_turns=max_turns,
                run_config=build_context_run_config(agent_config) if agent_config is not None else None,
            )
            segment_scope = _next_subagent_segment_scope(context)
            buffers: dict[str, DeltaBuffer] = {}
            try:
                async for event in iter_interruptible_events(
                    stream,
                    session_id=snapshot.session_id,
                    agent_instance_id=context.agent_instance_id,
                    current_agent_name=child_agent.name,
                    segment_scope=segment_scope,
                ):
                    track_delta(buffers, event)
                    _publish_event(snapshot.session_id, _tag_nested(event, snapshot))
                    await _update_progress_from_event(snapshot, event)
                buffers.clear()
            except (InterruptSignal, asyncio.CancelledError):
                # Both paths end the subagent turn mid-flight; emit boundary +
                # done so the parent's live projection sees in-flight nested
                # deltas as finalized and clients don't get a dangling stream
                # on reconnect. Partial buffers are dropped, not persisted.
                boundary_events = incomplete_segment_events(buffers, agent_name=child_agent.name)
                await discard_partial_stream(stream, buffers, log_label="subagent")
                for evt in boundary_events:
                    _publish_event(snapshot.session_id, _tag_nested(evt, snapshot))
                _publish_event(snapshot.session_id, _tag_nested(
                    DoneEvent(created_at=datetime.now(), agent_name=child_agent.name),
                    snapshot,
                ))
                raise
            except StreamIdleTimeout as exc:
                await discard_partial_stream(stream, buffers, log_label="subagent")
                raise RuntimeError(str(exc)) from exc
            except Exception:
                await discard_partial_stream(stream, buffers, log_label="subagent")
                raise
            return stream

        async def _has_background() -> bool:
            return await _subagent_has_running_background_work(
                snapshot.session_id, context.agent_instance_id,
            )

        await run_until_idle(
            session_id=snapshot.session_id,
            agent_instance_id=context.agent_instance_id,
            initial_content=text_input_content(snapshot.brief),
            run_turn=_run_turn,
            has_background_work=_has_background,
        )

        completed = await agent_subordinates.complete_subagent_task(
            snapshot.run_id,
            await _subagent_assistant_output(snapshot),
        )
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
        error_event = ErrorEvent(
            created_at=datetime.now(),
            agent_name=child_agent.name,
            message=f"Subagent failed: {exc}",
        )
        tagged_error = _tag_nested(error_event, snapshot)
        _publish_event(snapshot.session_id, tagged_error)
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
            await _mark_parent_session_stopped(snapshot.session_id)
            await _notify_session_idle(snapshot.session_id)


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


def _next_subagent_segment_scope(context: AgentRuntimeContext) -> str:
    owner = context.agent_instance_id or context.agent_code or "subagent"
    return next_segment_scope(owner)


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
        _publish_event(latest.session_id, _task_event(latest))


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
    except Exception:
        logger.exception("failed to queue parent notification for subagent task: %s", snapshot.run_id)
        return
    if context is not None:
        try:
            await signal_target_notifications(snapshot.parent_agent_instance_id)
        except Exception:
            logger.exception("failed to signal parent notification: %s", snapshot.run_id)


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


async def _notify_session_idle(session_id: str) -> None:
    try:
        from core.runtime.session import get_agent_pool

        await get_agent_pool().notify_idle(session_id)
    except Exception:
        logger.debug("failed to notify session idle: %s", session_id, exc_info=True)


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


async def _subagent_assistant_output(snapshot: AgentSubordinateTaskSnapshot) -> str:
    async with get_async_session() as sess:
        stored_items = await fetch_stored_items(sess, snapshot.session_id)

    sections: list[str] = []
    for stored in stored_items:
        if (
            stored.owner_code != snapshot.agent_code
            or stored.nested_for != snapshot.parent_agent_code
            or stored.nested_call_id != snapshot.nested_call_id
        ):
            continue
        text = _assistant_message_text(stored.item)
        if text:
            sections.append(text)
    return _truncate_subagent_result("\n\n".join(sections).strip())


def _assistant_message_text(item: dict[str, Any]) -> str:
    if item.get("type") == "message" and item.get("role") == "assistant":
        return extract_message_text(item.get("content")).strip()
    return ""


def _truncate_subagent_result(text: str) -> str:
    if len(text) <= _SUBAGENT_RESULT_MAX_CHARS:
        return text
    return (
        text[:_SUBAGENT_RESULT_MAX_CHARS].rstrip()
        + "\n\n[Subagent result truncated; view the session transcript for the full output.]"
    )
