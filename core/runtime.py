"""Per-conversation Agent runtime: turn execution and pool lifecycle."""

import asyncio
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from agents import Runner, TResponseInputItem
from agents.stream_events import AgentUpdatedStreamEvent
from openai.types.responses import (
    EasyInputMessageParam,
    ResponseInputMessageContentListParam,
    ResponseInputTextParam,
)

from config import get_config
from core.agents import AgentRegistry, AgentToolSnapshot, SessionAgentGraph
from core.context import AgentRuntimeContext
from core.events import event_from_sdk_stream
from core.session import Z3r0Session
from core.subordinates import cancel_session_subagent_runs
from database import get_engine
from logger import get_logger
from schema.agent_event_schema import (
    AgentEventSchema,
    DoneEvent,
    ErrorEvent,
    TextCompleteEvent,
    TextDeltaEvent,
    ThinkingCompleteEvent,
    ThinkingDeltaEvent,
    UserMessageEvent,
)
from schema.agent_notification_schema import AgentNotificationKind, AgentNotificationSnapshot
from service import agent_notification_service


logger = get_logger(__name__)


@dataclass
class _DeltaBuffer:
    is_thinking: bool
    item_id: str
    content: str = ""
    complete: bool = False


class AgentSession:
    def __init__(self, session_id: str, registry: AgentRegistry) -> None:
        self.session_id = session_id
        self._registry = registry
        self._turn_lock = asyncio.Lock()
        self._current_task: asyncio.Task | None = None
        self._main_agent_code: str = ""
        self._tool_snapshot: AgentToolSnapshot | None = None
        self._agent_graph: SessionAgentGraph | None = None

    def is_running(self) -> bool:
        task = self._current_task
        return task is not None and not task.done()

    async def stream_turn(
        self, text: str, agent_code: str, context: AgentRuntimeContext,
    ) -> AsyncIterator[AgentEventSchema]:
        async with self._turn_lock:
            task = asyncio.current_task()
            self._current_task = task
            try:
                async for event in self._run_turn(text, agent_code, context):
                    yield event
            finally:
                if self._current_task is task:
                    self._current_task = None

    async def stream_notification(
        self, context: AgentRuntimeContext,
    ) -> AsyncIterator[AgentEventSchema]:
        async with self._turn_lock:
            notification = await agent_notification_service.claim_next_pending_notification(
                session_id=self.session_id,
            )
            if notification is None:
                return
            agent_code = notification.target_agent_code
            notification_context = _context_for_notification(context, notification)
            task = asyncio.current_task()
            self._current_task = task
            saw_error = False
            try:
                async for event in self._run_turn(
                    _notification_prompt(notification),
                    agent_code,
                    notification_context,
                    emit_user_message=False,
                ):
                    if isinstance(event, ErrorEvent):
                        saw_error = True
                    yield event
            except asyncio.CancelledError:
                await agent_notification_service.release_notification(notification.id)
                raise
            except GeneratorExit:
                await agent_notification_service.release_notification(notification.id)
                raise
            except Exception as exc:
                await agent_notification_service.fail_notification(notification.id, str(exc) or "notification handling failed")
                raise
            else:
                if saw_error:
                    await agent_notification_service.fail_notification(notification.id, "notification handling produced an error")
                else:
                    await agent_notification_service.complete_notification(notification.id)
            finally:
                if self._current_task is task:
                    self._current_task = None

    async def has_pending_notification(self) -> bool:
        return await agent_notification_service.has_pending_notification(
            session_id=self.session_id,
        )

    async def interrupt(self) -> bool:
        task = self._current_task
        if task is None or task.done():
            return False
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        return True

    async def cancel_all(self) -> bool:
        task = self._current_task
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        canceled_subagents = await cancel_session_subagent_runs(self.session_id)
        canceled_notifications = await agent_notification_service.cancel_session_notifications(
            self.session_id,
            "Agent session tasks canceled by user.",
        )
        return canceled_subagents or bool(canceled_notifications)

    async def shutdown(self) -> None:
        await self.cancel_all()
        self.close()

    def close(self) -> None:
        self._dispose_agent_graph()

    def uses_sandbox_container(self, container_id: int) -> bool:
        return self._tool_snapshot is not None and self._tool_snapshot.sandbox_container_id == container_id

    async def invalidate_tool_binding(self) -> None:
        await self.cancel_all()
        self._tool_snapshot = None
        self._dispose_agent_graph()

    async def _run_turn(
        self,
        text: str,
        agent_code: str,
        context: AgentRuntimeContext,
        *,
        emit_user_message: bool = True,
    ) -> AsyncIterator[AgentEventSchema]:
        graph = self._ensure_agent_graph(agent_code, context)
        context.agent_code = agent_code
        agent = graph.get(agent_code)
        if emit_user_message:
            yield UserMessageEvent(created_at=datetime.now(), text=text, target_agent_code=agent_code)

        memory_session = Z3r0Session(
            session_id=self.session_id,
            engine=get_engine(),
            viewing_agent_code=agent_code,
            agent_code_to_name=graph.code_to_name(),
        )
        # SDK stream events converge into one queue for this turn
        queue: asyncio.Queue[AgentEventSchema | None] = asyncio.Queue()

        result_holder: dict[str, Any] = {"result": None}
        buffers: dict[str, _DeltaBuffer] = {}

        async def _consume_main() -> None:
            try:
                max_turns = get_config().agent_runtime.main_max_turns
                stream = Runner.run_streamed(
                    starting_agent=agent,
                    session=memory_session,
                    input=_build_user_input(text),
                    context=context,
                    max_turns=max_turns,
                )
                result_holder["result"] = stream
                async for sdk_event in stream.stream_events():
                    if isinstance(sdk_event, AgentUpdatedStreamEvent):
                        continue
                    event = event_from_sdk_stream(sdk_event, agent.name)
                    if event is not None:
                        queue.put_nowait(event)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.exception("agent stream failed: %s", exc)
                queue.put_nowait(ErrorEvent(created_at=datetime.now(), agent_name=agent.name, message=str(exc)))
            finally:
                queue.put_nowait(None)

        main_task = asyncio.create_task(_consume_main(), name="agent-main-stream")

        try:
            while True:
                event = await queue.get()
                if event is None:
                    break
                _track_delta(buffers, event)
                yield event
            yield DoneEvent(created_at=datetime.now(), agent_name=agent.name)
        except asyncio.CancelledError:
            if not main_task.done():
                main_task.cancel()
            raise
        finally:
            if not main_task.done():
                main_task.cancel()
                try:
                    await main_task
                except (asyncio.CancelledError, Exception):
                    pass
            await _flush_partial_context(result_holder["result"], memory_session, buffers)

    def _ensure_agent_graph(self, agent_code: str, context: AgentRuntimeContext) -> SessionAgentGraph:
        tool_snapshot = AgentToolSnapshot.from_context(context)
        if (
            self._agent_graph is None
            or self._main_agent_code != agent_code
            or self._tool_snapshot != tool_snapshot
        ):
            self._dispose_agent_graph()
            self._main_agent_code = agent_code
            self._tool_snapshot = tool_snapshot
            self._agent_graph = self._registry.bind(tool_snapshot)
            logger.debug(
                "agent graph bound session=%s agent=%s knowledge_generation=%d sandbox=%s generation=%d",
                self.session_id,
                agent_code,
                tool_snapshot.knowledge_generation,
                tool_snapshot.sandbox_container_id,
                tool_snapshot.sandbox_container_generation,
            )
        return self._agent_graph

    def _dispose_agent_graph(self) -> None:
        if self._agent_graph is None:
            return
        self._agent_graph.close()
        self._agent_graph = None
        self._main_agent_code = ""


@dataclass
class _PooledSession:
    session: AgentSession
    last_used_at: float = field(default_factory=time.monotonic)


class AgentSessionPool:
    def __init__(self, registry: AgentRegistry | None = None) -> None:
        cfg = get_config().agent_pool
        self._registry = registry or AgentRegistry()
        self._max_size = cfg.max_size
        self._ttl = cfg.ttl_seconds
        self._sweep_interval = cfg.sweep_interval_seconds
        self._pool: dict[str, _PooledSession] = {}
        self._sweeper_task: asyncio.Task | None = None

    @property
    def registry(self) -> AgentRegistry:
        return self._registry

    async def start(self) -> None:
        if self._sweeper_task is not None and not self._sweeper_task.done():
            return
        self._sweeper_task = asyncio.create_task(self._sweep_loop(), name="agent-pool-sweeper")
        logger.debug(
            "agent pool started (ttl=%ds, interval=%ds, max_size=%d)",
            self._ttl, self._sweep_interval, self._max_size,
        )

    async def stop(self) -> None:
        task, self._sweeper_task = self._sweeper_task, None
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        entries = list(self._pool.values())
        self._pool.clear()
        await asyncio.gather(*(entry.session.shutdown() for entry in entries), return_exceptions=True)
        logger.debug("agent pool stopped")

    def get_or_create(self, session_id: str) -> AgentSession:
        entry = self._pool.get(session_id)
        if entry is None:
            entry = _PooledSession(session=AgentSession(session_id, self._registry))
            self._pool[session_id] = entry
            logger.debug("agent pool created session=%s", session_id)
            self._enforce_capacity()
        else:
            entry.last_used_at = time.monotonic()
        return entry.session

    async def discard(self, session_id: str) -> None:
        entry = self._pool.pop(session_id, None)
        if entry is None:
            return
        await entry.session.shutdown()
        logger.debug("agent pool discarded session=%s", session_id)

    async def try_interrupt(self, session_id: str) -> bool:
        entry = self._pool.get(session_id)
        if entry is None:
            return False
        return await entry.session.interrupt()

    async def cancel_all(self, session_id: str) -> bool:
        entry = self._pool.get(session_id)
        if entry is None:
            canceled_subagents = await cancel_session_subagent_runs(session_id)
            canceled_notifications = await agent_notification_service.cancel_session_notifications(
                session_id,
                "Agent session tasks canceled by user.",
            )
            return canceled_subagents or bool(canceled_notifications)
        return await entry.session.cancel_all()

    async def invalidate_tool_bindings(self, container_id: int | None = None) -> None:
        entries = [
            entry for entry in self._pool.values()
            if container_id is None or entry.session.uses_sandbox_container(container_id)
        ]
        if not entries:
            return
        await asyncio.gather(
            *(entry.session.invalidate_tool_binding() for entry in entries),
            return_exceptions=True,
        )
        logger.debug("agent pool invalidated tool bindings container=%s count=%d", container_id, len(entries))

    async def _sweep_loop(self) -> None:
        while True:
            try:
                await asyncio.sleep(self._sweep_interval)
                self._sweep_expired(time.monotonic())
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("agent pool sweep iteration failed")

    def _sweep_expired(self, now: float) -> None:
        if self._ttl <= 0:
            return
        expired = [
            sid for sid, entry in self._pool.items()
            if not entry.session.is_running() and now - entry.last_used_at > self._ttl
        ]
        for sid in expired:
            entry = self._pool.pop(sid)
            entry.session.close()
            logger.debug("agent pool evicted idle session=%s", sid)

    def _enforce_capacity(self) -> None:
        # only idle entries are evicted; running sessions may briefly exceed the cap
        overflow = len(self._pool) - self._max_size
        if overflow <= 0:
            return
        idle = sorted(
            ((sid, entry) for sid, entry in self._pool.items() if not entry.session.is_running()),
            key=lambda kv: kv[1].last_used_at,
        )
        for sid, _ in idle[:overflow]:
            entry = self._pool.pop(sid)
            entry.session.close()
            logger.debug("agent pool evicted LRU session=%s", sid)


_pool: AgentSessionPool | None = None


def get_agent_pool() -> AgentSessionPool:
    global _pool
    if _pool is None:
        _pool = AgentSessionPool()
    return _pool


def get_agent_registry() -> AgentRegistry:
    return get_agent_pool().registry


_DELTA_TYPES: tuple[type, ...] = (TextDeltaEvent, ThinkingDeltaEvent)
_COMPLETE_TYPES: tuple[type, ...] = (TextCompleteEvent, ThinkingCompleteEvent)


def _build_user_input(text: str) -> list[TResponseInputItem]:
    text_item: ResponseInputTextParam = {"type": "input_text", "text": text}
    content: ResponseInputMessageContentListParam = [text_item]
    message: EasyInputMessageParam = {"type": "message", "role": "user", "content": content}
    return [message]


def _notification_prompt(notification: AgentNotificationSnapshot) -> str:
    if notification.kind == AgentNotificationKind.SUBAGENT_FINISHED:
        payload = notification.payload
        status = str(payload.get("status") or "")
        agent_name = str(payload.get("agent_name") or payload.get("agent_code") or "subagent")
        run_id = str(payload.get("run_id") or notification.run_id)
        brief = str(payload.get("brief") or "")
        result = str(payload.get("result") or "")
        error = str(payload.get("error") or "")
        body = result if status == "completed" else error
        return (
            "# Internal Subagent Completion Notification\n\n"
            "A delegated subagent task has reached a terminal state. This is an internal runtime notification, "
            "not a new user message. Do not mention implementation details about notifications.\n\n"
            f"- run_id: {run_id}\n"
            f"- subagent: {agent_name}\n"
            f"- status: {status}\n"
            f"- original brief: {brief}\n\n"
            "## Subagent final output\n\n"
            f"{body}\n\n"
            "Integrate this result into the current task context and respond to the user only if there is "
            "a useful coordination update, conclusion, or next action to report."
        )
    return (
        "# Internal Agent Notification\n\n"
        f"Notification kind: {notification.kind.value}\n"
        f"Payload: {notification.payload}"
    )


def _context_for_notification(
    base: AgentRuntimeContext,
    notification: AgentNotificationSnapshot,
) -> AgentRuntimeContext:
    return AgentRuntimeContext(
        session_id=base.session_id,
        user=base.user,
        agent_code=notification.target_agent_code,
        knowledge_generation=base.knowledge_generation,
        sandbox_container_id=notification.sandbox_container_id,
        sandbox_container_generation=notification.sandbox_container_generation,
        sandbox_skill_metadata=notification.sandbox_skill_metadata,
    )


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
    """On cancellation, persist accumulated deltas so the next turn sees the truncated context."""
    if result is None or getattr(result, "is_complete", True):
        return
    try:
        result.cancel(mode="immediate")
    except Exception:
        logger.warning("failed to cancel SDK stream", exc_info=True)
    items: list[TResponseInputItem] = [
        _partial_reasoning_item(buf) if buf.is_thinking else _partial_assistant_item(buf)
        for buf in buffers.values() if buf.content
    ]
    if not items:
        return
    try:
        await memory_session.add_items(items)
    except Exception:
        logger.warning("failed to inject partial assistant context", exc_info=True)


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
