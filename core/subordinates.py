"""Persistent background execution for delegated subagent tasks."""

from __future__ import annotations

import asyncio
from collections import defaultdict
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any

from agents import Agent, RunContextWrapper, Runner, Tool, TResponseInputItem, function_tool
from agents.stream_events import AgentUpdatedStreamEvent

from config import get_config
from core.context import AgentRuntimeContext
from core.events import event_from_sdk_stream
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
from schema.agent_subordinate_schema import AgentSubordinateTaskSnapshot, AgentSubordinateTaskToolResponse
from service import agent_subordinate_service


logger = get_logger(__name__)


@dataclass
class _DeltaBuffer:
    is_thinking: bool
    item_id: str
    content: str = ""


@dataclass
class _SubagentJob:
    task: asyncio.Task[None]
    done: asyncio.Event


_jobs: dict[str, _SubagentJob] = {}
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
        try:
            snapshot = await asyncio.shield(starter)
        except asyncio.CancelledError:
            starter.add_done_callback(_log_subagent_start_result)
            raise
        return _tool_response(task=snapshot, message="subagent task started")

    async def read_subagent_task(ctx: RunContextWrapper[AgentRuntimeContext], run_id: str) -> str:
        """Read current status/result/error/progress for a subagent task."""
        snapshot = await _resolve_task(ctx, run_id)
        if snapshot is None:
            return _tool_response(message="subagent task not found")
        return _tool_response(task=snapshot)

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

    return [
        function_tool(
            start_subagent_task,
            name_override="start_subagent_task",
            description_override=(
                "Start a configured subagent by code and return a persistent run id. "
                f"Allowed agent_code values: {allowed_codes}."
            ),
        ),
        function_tool(
            wait_subagent_task,
            name_override="wait_subagent_task",
            description_override="Wait for a persistent subagent run id, returning result if done or current status if still running.",
        ),
        function_tool(
            read_subagent_task,
            name_override="read_subagent_task",
            description_override="Read current status, result, error, and progress for a persistent subagent run id.",
        ),
        function_tool(
            cancel_subagent_task,
            name_override="cancel_subagent_task",
            description_override="Cancel a persistent subagent run id when practical.",
        ),
    ]


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
        agent_code=agent_code,
        agent_name=code_to_name.get(agent_code, child_agent.name),
        brief=brief,
        nested_call_id=nested_call_id,
        owner_id=context.user.id,
    )
    runtime_context = _clone_context_for_background(context)
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
    _jobs[snapshot.run_id] = _SubagentJob(task=task, done=done)
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
    job = _jobs.get(snapshot.run_id)
    if job is not None and not job.task.done():
        job.task.cancel()
    latest = await agent_subordinate_service.cancel_subagent_task_record(snapshot.run_id, _CANCEL_MESSAGE)
    snapshot = latest or snapshot
    await publish_subagent_event(snapshot.session_id, _task_event(snapshot))
    return snapshot


async def cancel_session_subagent_runs(session_id: str) -> None:
    matching: list[_SubagentJob] = []
    for run_id, job in list(_jobs.items()):
        snapshot = await agent_subordinate_service.get_subagent_task_internal(run_id)
        if snapshot is not None and snapshot.session_id == session_id:
            matching.append(job)
    for job in matching:
        if not job.task.done():
            job.task.cancel()
    if matching:
        await asyncio.gather(*(job.task for job in matching), return_exceptions=True)


async def start_subagent_runtime() -> None:
    await agent_subordinate_service.mark_stale_running_subagent_tasks_failed()


async def stop_subagent_runtime() -> None:
    jobs = list(_jobs.values())
    _jobs.clear()
    for job in jobs:
        if not job.task.done():
            job.task.cancel()
    if jobs:
        await asyncio.gather(*(job.task for job in jobs), return_exceptions=True)


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
        stream = Runner.run_streamed(
            starting_agent=child_agent,
            input=snapshot.brief,
            session=memory_session,
            context=context,
            max_turns=max_turns,
        )
        result = stream
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

        completed = await agent_subordinate_service.complete_subagent_task(snapshot.run_id, _final_text(stream))
        if completed is not None:
            await publish_subagent_event(completed.session_id, _task_event(completed))
    except asyncio.CancelledError:
        await _flush_partial_context(result, memory_session, buffers)
        canceled = await agent_subordinate_service.cancel_subagent_task_record(snapshot.run_id, _CANCEL_MESSAGE)
        if canceled is not None:
            await publish_subagent_event(canceled.session_id, _task_event(canceled))
    except Exception as exc:
        logger.exception("subagent task failed: %s", snapshot.run_id)
        await _flush_partial_context(result, memory_session, buffers)
        tagged_error = _tag_nested(ErrorEvent(agent_name=child_agent.name, message=f"Subagent failed: {exc}"), snapshot)
        await publish_subagent_event(snapshot.session_id, tagged_error)
        failed = await agent_subordinate_service.fail_subagent_task(snapshot.run_id, str(exc) or "subagent failed")
        if failed is not None:
            await publish_subagent_event(failed.session_id, _task_event(failed))
    finally:
        done.set()
        current = _jobs.get(snapshot.run_id)
        if current is not None and current.done is done:
            _jobs.pop(snapshot.run_id, None)


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
        agent_name=snapshot.agent_name,
        nested_for=snapshot.parent_agent_code,
        nested_call_id=snapshot.nested_call_id,
        run_id=snapshot.run_id,
        parent_agent_code=snapshot.parent_agent_code,
        agent_code=snapshot.agent_code,
        status=snapshot.status,
        result=snapshot.result,
        error=snapshot.error,
        progress=snapshot.progress,
    )


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
        sandbox_container_id=context.sandbox_container_id,
        sandbox_container_generation=context.sandbox_container_generation,
        sandbox_skill_metadata=context.sandbox_skill_metadata,
    )
    return RunContextWrapper(context=cloned) if isinstance(context, RunContextWrapper) else cloned


def _track_delta(buffers: dict[str, _DeltaBuffer], event: AgentEventSchema) -> None:
    if isinstance(event, _DELTA_TYPES):
        buf = buffers.get(event.item_id)
        if buf is None:
            buf = _DeltaBuffer(is_thinking=isinstance(event, ThinkingDeltaEvent), item_id=event.item_id)
            buffers[event.item_id] = buf
        buf.content += event.delta
    elif isinstance(event, _COMPLETE_TYPES):
        buffers.pop(event.item_id, None)


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
        "status": "incomplete",
    }


def _partial_reasoning_item(buf: _DeltaBuffer) -> TResponseInputItem:
    return {
        "id": f"partial_{buf.item_id}",
        "type": "reasoning",
        "summary": [{"type": "summary_text", "text": buf.content}],
        "status": "incomplete",
    }


def _final_text(result: Any) -> str:
    output = getattr(result, "final_output", None)
    if output is None:
        return ""
    return output if isinstance(output, str) else str(output)
