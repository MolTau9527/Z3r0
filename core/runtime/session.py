"""Per-conversation Agent runtime: turn execution and pool lifecycle."""

import asyncio
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from agents import Runner

from config import get_config
from core.agent.registry import AgentRegistry, AgentToolSnapshot, SessionAgentGraph
from core.runtime.context import AgentRuntimeContext, main_agent_instance_id
from core.runtime.input_items import build_user_message_item, display_text_from_content
from core.runtime.live_projection import LiveEventProjection
from core.runtime.partial_context import DeltaBuffer, flush_partial_context, track_delta
from core.runtime.streaming import StreamIdleTimeout, next_segment_scope
from core.task_runtime import InterruptSignal, iter_interruptible_events, run_until_idle
from core.conversation.store import Z3r0Session
from core.delegation.subagents import cancel_sandbox_subagent_runs, cancel_session_subagent_runs
from core.sandbox.command_jobs import cancel_sandbox_async_commands, cancel_session_async_sandbox_commands
from database import get_engine
from logger import get_logger
from schema.agent.events import (
    AgentEventSchema,
    AgentInputPart,
    DoneEvent,
    ErrorEvent,
    RunStateEvent,
    UserMessageEvent,
)
from schema.agent.notifications import AgentNotificationSnapshot
from service.agent import notifications as agent_notifications


logger = get_logger(__name__)

_SUBSCRIBER_REBASE_THRESHOLD = 512


class AgentSession:
    def __init__(self, session_id: str, registry: AgentRegistry) -> None:
        self.session_id = session_id
        self._registry = registry
        self._start_lock = asyncio.Lock()
        self._turn_lock = asyncio.Lock()
        self._current_task: asyncio.Task | None = None
        self._subscribers: set[asyncio.Queue[AgentEventSchema]] = set()
        self._live_projection = LiveEventProjection()
        self._main_agent_code: str = ""
        self._tool_snapshot: AgentToolSnapshot | None = None
        self._agent_graph: SessionAgentGraph | None = None

    def is_running(self) -> bool:
        task = self._current_task
        return task is not None and not task.done()

    def has_subscribers(self) -> bool:
        return bool(self._subscribers)

    async def subscribe(
        self,
        include: Callable[[AgentEventSchema], bool] | None = None,
    ) -> asyncio.Queue[AgentEventSchema]:
        snapshot = self._live_projection.snapshot(include)
        queue: asyncio.Queue[AgentEventSchema] = asyncio.Queue()
        for event in snapshot:
            queue.put_nowait(event)
        self._subscribers.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue[AgentEventSchema]) -> None:
        self._subscribers.discard(queue)

    async def start_turn(self, content: list[AgentInputPart], agent_code: str, context: AgentRuntimeContext) -> None:
        async with self._start_lock:
            if self.is_running():
                await self.interrupt()
            await _mark_session_running(
                self.session_id,
                agent_code=agent_code,
                sandbox_container_id=context.sandbox_container_id,
                sandbox_container_generation=context.sandbox_container_generation,
            )
            self._publish_run_state(True)
            task = asyncio.create_task(
                self._run_background_turn(content, agent_code, context),
                name=f"agent-turn-{self.session_id}",
            )
            self._current_task = task

    async def start_notification_recovery(self, context: AgentRuntimeContext) -> bool:
        """Start processing pending notifications after a server restart.

        Unlike ``start_turn``, this does not run an initial model turn; it
        goes directly to the notification consumption loop via ``run_until_idle``.
        """
        async with self._start_lock:
            if self.is_running():
                return False
            if not await agent_notifications.has_pending_main_agent_notification(
                session_id=self.session_id,
            ):
                return False
            await _mark_session_running(
                self.session_id,
                agent_code=context.agent_code,
                sandbox_container_id=context.sandbox_container_id,
                sandbox_container_generation=context.sandbox_container_generation,
            )
            self._publish_run_state(True)
            task = asyncio.create_task(
                self._run_background_recovery(context),
                name=f"agent-recovery-{self.session_id}",
            )
            self._current_task = task
            return True

    async def _run_background_recovery(self, context: AgentRuntimeContext) -> None:
        async with self._turn_lock:
            task = asyncio.current_task()
            self._current_task = task
            try:
                context.agent_code = context.agent_code or ""
                if not context.agent_instance_id:
                    context.agent_instance_id = main_agent_instance_id(
                        context.session_id, context.user.id, context.agent_code,
                    )

                async def _run_turn(
                    turn_content: list[AgentInputPart],
                    notification: AgentNotificationSnapshot | None,
                ) -> Any:
                    return await self._execute_turn(
                        turn_content, context.agent_code, context,
                        notification=notification,
                        emit_user_message=False,
                    )

                async def _has_background() -> bool:
                    return await _has_active_session_runtime(self.session_id)

                await run_until_idle(
                    session_id=self.session_id,
                    agent_instance_id=context.agent_instance_id,
                    initial_content=None,
                    run_turn=_run_turn,
                    has_background_work=_has_background,
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.exception("agent notification recovery failed session=%s", self.session_id)
                await _force_mark_session_stopped(self.session_id, error=str(exc) or "notification recovery failed")
                await self._publish(ErrorEvent(created_at=datetime.now(), message=str(exc) or "notification recovery failed"))
                await self._publish(DoneEvent(created_at=datetime.now()))
            finally:
                await _mark_session_stopped(self.session_id)
                if self._current_task is task:
                    self._current_task = None
                await self._publish_idle_if_inactive()

    async def interrupt(self) -> bool:
        task = self._current_task
        if task is None or task.done():
            return False
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        await _mark_session_stopped(self.session_id)
        await self._publish(DoneEvent(created_at=datetime.now()))
        await self._publish_idle_if_inactive()
        return True

    async def cancel_all(self) -> bool:
        task = self._current_task
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            await _mark_session_stopped(self.session_id)
            await self._publish(DoneEvent(created_at=datetime.now()))
        canceled_subagents = await cancel_session_subagent_runs(self.session_id)
        canceled_commands = await cancel_session_async_sandbox_commands(self.session_id)
        canceled_notifications = await agent_notifications.cancel_session_notifications(
            self.session_id,
            "Agent session tasks canceled by user.",
        )
        await _force_mark_session_stopped(self.session_id)
        self._publish_run_state(False)
        return canceled_subagents or canceled_commands or bool(canceled_notifications)

    async def shutdown(self) -> None:
        await self.cancel_all()
        await self.close()

    async def close(self) -> None:
        await self._dispose_agent_graph()

    def uses_sandbox_container(self, container_id: int) -> bool:
        return self._tool_snapshot is not None and self._tool_snapshot.sandbox_container_id == container_id

    async def invalidate_tool_binding(self) -> None:
        await self.cancel_all()
        self._tool_snapshot = None
        await self._dispose_agent_graph()

    async def _execute_turn(
        self,
        content: list[AgentInputPart],
        agent_code: str,
        context: AgentRuntimeContext,
        *,
        notification: AgentNotificationSnapshot | None = None,
        emit_user_message: bool = True,
    ) -> Any:
        """Run a single agent turn, publishing events directly.

        Returns the SDK stream result on normal completion.
        Raises ``InterruptSignal`` when preempted by a pending notification.
        """
        if notification is not None:
            turn_context = _context_for_notification(context, notification)
            turn_agent_code = notification.target_agent_code
        else:
            turn_context = context
            turn_agent_code = agent_code

        graph = await self._ensure_agent_graph(turn_agent_code, turn_context)
        agent = graph.get(turn_agent_code)
        turn_scope = _next_turn_scope(turn_context)

        if emit_user_message:
            await self._publish(UserMessageEvent(
                created_at=datetime.now(),
                content=content,
                display_text=display_text_from_content(content),
                target_agent_code=turn_agent_code,
            ))

        memory_session = Z3r0Session(
            session_id=self.session_id,
            engine=get_engine(),
            viewing_agent_code=turn_agent_code,
            agent_code_to_name=graph.code_to_name(),
            nested_for_agent_code=turn_context.nested_for_agent_code,
            nested_call_id=turn_context.nested_call_id,
        )

        user_input = [build_user_message_item(content)]
        if emit_user_message:
            agent_config = get_config().agents.get(turn_agent_code)
            if agent_config is not None:
                await memory_session.compact_if_needed(
                    agent_config=agent_config,
                    incoming_items=user_input,
                )

        stream = Runner.run_streamed(
            starting_agent=agent,
            session=memory_session,
            input=user_input,
            context=turn_context,
            max_turns=get_config().agent_runtime.main_max_turns,
        )

        def _tag(event: AgentEventSchema) -> AgentEventSchema:
            return _tag_notification_event(event, turn_context) if notification else event

        buffers: dict[str, DeltaBuffer] = {}
        stream_error: ErrorEvent | None = None
        try:
            async for event in iter_interruptible_events(
                stream,
                session_id=self.session_id,
                agent_instance_id=turn_context.agent_instance_id,
                current_agent_name=agent.name,
                segment_scope=turn_scope,
            ):
                track_delta(buffers, event)
                await self._publish(_tag(event))
            buffers.clear()
        except InterruptSignal:
            await flush_partial_context(stream, memory_session, buffers, log_label="agent")
            raise
        except asyncio.CancelledError:
            await flush_partial_context(stream, memory_session, buffers, log_label="agent")
            raise
        except StreamIdleTimeout as exc:
            await flush_partial_context(stream, memory_session, buffers, log_label="agent")
            logger.warning(
                "agent stream idle timeout session=%s agent=%s phase=%s timeout=%d",
                self.session_id, turn_agent_code, exc.phase, exc.timeout_seconds,
            )
            stream_error = ErrorEvent(created_at=datetime.now(), agent_name=agent.name, message=str(exc))
        except Exception as exc:
            await flush_partial_context(stream, memory_session, buffers, log_label="agent")
            logger.exception("agent stream failed session=%s: %s", self.session_id, exc)
            stream_error = ErrorEvent(created_at=datetime.now(), agent_name=agent.name, message=str(exc))

        if stream_error is not None:
            await self._publish(_tag(stream_error))
        await self._publish(_tag(DoneEvent(created_at=datetime.now(), agent_name=agent.name)))
        return stream

    async def _ensure_agent_graph(self, agent_code: str, context: AgentRuntimeContext) -> SessionAgentGraph:
        tool_snapshot = AgentToolSnapshot.from_context(context)
        if (
            self._agent_graph is None
            or self._main_agent_code != agent_code
            or self._tool_snapshot != tool_snapshot
        ):
            await self._dispose_agent_graph()
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

    async def _dispose_agent_graph(self) -> None:
        if self._agent_graph is None:
            return
        await self._agent_graph.close()
        self._agent_graph = None
        self._main_agent_code = ""

    async def _run_background_turn(
        self,
        content: list[AgentInputPart],
        agent_code: str,
        context: AgentRuntimeContext,
    ) -> None:
        async with self._turn_lock:
            task = asyncio.current_task()
            self._current_task = task
            canceled = False
            try:
                context.agent_code = agent_code
                if not context.agent_instance_id:
                    context.agent_instance_id = main_agent_instance_id(
                        context.session_id, context.user.id, agent_code,
                    )

                is_initial = True

                async def _run_turn(
                    turn_content: list[AgentInputPart],
                    notification: AgentNotificationSnapshot | None,
                ) -> Any:
                    nonlocal is_initial
                    emit = is_initial and notification is None
                    is_initial = False
                    return await self._execute_turn(
                        turn_content, agent_code, context,
                        notification=notification,
                        emit_user_message=emit,
                    )

                async def _has_background() -> bool:
                    return await _has_active_session_runtime(self.session_id)

                await run_until_idle(
                    session_id=self.session_id,
                    agent_instance_id=context.agent_instance_id,
                    initial_content=content,
                    run_turn=_run_turn,
                    has_background_work=_has_background,
                )
            except asyncio.CancelledError:
                canceled = True
                raise
            except Exception as exc:
                logger.exception("agent background turn failed session=%s", self.session_id)
                await _force_mark_session_stopped(self.session_id, error=str(exc) or "agent turn failed")
                await self._publish(ErrorEvent(created_at=datetime.now(), message=str(exc) or "agent turn failed"))
                await self._publish(DoneEvent(created_at=datetime.now()))
            finally:
                if not canceled:
                    await _mark_session_stopped(self.session_id)
                if self._current_task is task:
                    self._current_task = None
                if not canceled:
                    await self._publish_idle_if_inactive()

    def _publish_run_state(self, running: bool) -> None:
        event = RunStateEvent(created_at=datetime.now(), running=running)
        if running:
            self._live_projection.reset(event)
        else:
            self._live_projection.apply(event)
        for queue in tuple(self._subscribers):
            self._enqueue_or_rebase(queue, event)
        if not running:
            self._live_projection.reset(event)

    async def _publish_idle_if_inactive(self) -> None:
        if self.is_running() or await _has_active_session_runtime(self.session_id):
            return
        self._publish_run_state(False)

    async def _publish(self, event: AgentEventSchema) -> None:
        self.publish_external(event)

    def publish_external(self, event: AgentEventSchema) -> None:
        """Inject an event into the session's unified event bus.

        Used internally by ``_publish`` and externally via ``AgentSessionPool.publish``.
        Synchronous: projection.apply and put_nowait are non-blocking, and
        asyncio's single-threaded model guarantees atomicity.
        """
        self._live_projection.apply(event)
        for queue in tuple(self._subscribers):
            self._enqueue_or_rebase(queue, event)

    def _enqueue_or_rebase(self, queue: asyncio.Queue[AgentEventSchema], event: AgentEventSchema) -> None:
        if queue.qsize() < _SUBSCRIBER_REBASE_THRESHOLD:
            queue.put_nowait(event)
            return

        while True:
            try:
                queue.get_nowait()
            except asyncio.QueueEmpty:
                break

        snapshot = self._live_projection.snapshot()
        if isinstance(event, DoneEvent):
            snapshot.append(event)
        elif isinstance(event, RunStateEvent) and not event.running:
            snapshot = [item for item in snapshot if not isinstance(item, RunStateEvent)]
            snapshot.append(event)
        for item in snapshot:
            queue.put_nowait(item)


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
        self._lock = asyncio.Lock()

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

        async with self._lock:
            entries = list(self._pool.values())
            session_ids = list(self._pool.keys())
            self._pool.clear()
        await asyncio.gather(*(entry.session.shutdown() for entry in entries), return_exceptions=True)
        await _mark_sessions_stopped(session_ids)
        logger.debug("agent pool stopped")

    async def get_or_create(self, session_id: str) -> AgentSession:
        async with self._lock:
            session = self._get_or_create_locked(session_id)
            evicted = self._enforce_capacity_locked()
        await self._close_evicted(evicted, reason="LRU")
        return session

    def _get_or_create_locked(self, session_id: str) -> AgentSession:
        entry = self._pool.get(session_id)
        if entry is None:
            entry = _PooledSession(session=AgentSession(session_id, self._registry))
            self._pool[session_id] = entry
            logger.debug("agent pool created session=%s", session_id)
        else:
            entry.last_used_at = time.monotonic()
        return entry.session

    async def discard(self, session_id: str) -> None:
        async with self._lock:
            entry = self._pool.pop(session_id, None)
        if entry is None:
            await _force_mark_session_stopped(session_id)
            return
        await entry.session.shutdown()
        logger.debug("agent pool discarded session=%s", session_id)

    async def try_interrupt(self, session_id: str) -> bool:
        async with self._lock:
            entry = self._pool.get(session_id)
        if entry is None:
            return False
        return await entry.session.interrupt()

    async def subscribe(
        self,
        session_id: str,
        include: Callable[[AgentEventSchema], bool] | None = None,
    ) -> tuple[AgentSession, asyncio.Queue[AgentEventSchema]]:
        session = await self.get_or_create(session_id)
        return session, await session.subscribe(include)

    def publish(self, session_id: str, event: AgentEventSchema) -> None:
        """Route an external event to the session's unified event bus.

        Lock-free: dict.get and attribute assignment are atomic in asyncio's
        single-threaded model.  If no pool entry exists (no active subscriber),
        the event is silently dropped — it lives in the DB via the caller's
        Z3r0Session persistence.
        """
        entry = self._pool.get(session_id)
        if entry is None:
            return
        entry.last_used_at = time.monotonic()
        entry.session.publish_external(event)

    async def notify_idle(self, session_id: str) -> None:
        """Signal that external work (e.g., sub-agent tasks) may have finished.

        Delegates to ``AgentSession._publish_idle_if_inactive`` which checks
        ``is_running()`` and ``has_active_session_runtime()`` before publishing
        ``RunStateEvent(running=False)`` and resetting the projection.
        """
        entry = self._pool.get(session_id)
        if entry is not None:
            await entry.session._publish_idle_if_inactive()

    async def cancel_all(self, session_id: str) -> bool:
        async with self._lock:
            entry = self._pool.get(session_id)
        if entry is None:
            canceled_subagents = await cancel_session_subagent_runs(session_id)
            canceled_commands = await cancel_session_async_sandbox_commands(session_id)
            canceled_notifications = await agent_notifications.cancel_session_notifications(
                session_id,
                "Agent session tasks canceled by user.",
            )
            await _force_mark_session_stopped(session_id)
            return canceled_subagents or canceled_commands or bool(canceled_notifications)
        return await entry.session.cancel_all()

    async def invalidate_tool_bindings(self, container_id: int | None = None) -> None:
        async with self._lock:
            entries = [
                entry for entry in self._pool.values()
                if container_id is None or entry.session.uses_sandbox_container(container_id)
            ]
        tasks = [entry.session.invalidate_tool_binding() for entry in entries]
        if container_id is not None:
            tasks.extend([
                cancel_sandbox_subagent_runs(container_id),
                cancel_sandbox_async_commands(container_id),
            ])
        if not tasks:
            return
        await asyncio.gather(*tasks, return_exceptions=True)
        logger.debug("agent pool invalidated tool bindings container=%s count=%d", container_id, len(entries))

    async def _sweep_loop(self) -> None:
        while True:
            try:
                await asyncio.sleep(self._sweep_interval)
                async with self._lock:
                    expired = self._sweep_expired_locked(time.monotonic())
                await self._close_evicted(expired, reason="idle")
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("agent pool sweep iteration failed")

    def _sweep_expired_locked(self, now: float) -> list[tuple[str, _PooledSession]]:
        if self._ttl <= 0:
            return []
        expired = [
            sid for sid, entry in self._pool.items()
            if (
                not entry.session.is_running()
                and not entry.session.has_subscribers()
                and now - entry.last_used_at > self._ttl
            )
        ]
        evicted: list[tuple[str, _PooledSession]] = []
        for sid in expired:
            entry = self._pool.pop(sid)
            evicted.append((sid, entry))
        return evicted

    def _enforce_capacity_locked(self) -> list[tuple[str, _PooledSession]]:
        # only idle entries are evicted; running sessions may briefly exceed the cap
        overflow = len(self._pool) - self._max_size
        if overflow <= 0:
            return []
        idle = sorted(
            (
                (sid, entry)
                for sid, entry in self._pool.items()
                if not entry.session.is_running() and not entry.session.has_subscribers()
            ),
            key=lambda kv: kv[1].last_used_at,
        )
        evicted: list[tuple[str, _PooledSession]] = []
        for sid, _ in idle[:overflow]:
            entry = self._pool.pop(sid)
            evicted.append((sid, entry))
        return evicted

    async def _close_evicted(self, evicted: list[tuple[str, _PooledSession]], *, reason: str) -> None:
        if not evicted:
            return
        await asyncio.gather(*(entry.session.close() for _, entry in evicted), return_exceptions=True)
        for sid, _ in evicted:
            logger.debug("agent pool evicted %s session=%s", reason, sid)


_pool: AgentSessionPool | None = None


def get_agent_pool() -> AgentSessionPool:
    global _pool
    if _pool is None:
        _pool = AgentSessionPool()
    return _pool


def replace_agent_pool(pool: AgentSessionPool | None = None) -> AgentSessionPool:
    global _pool
    _pool = pool or AgentSessionPool()
    return _pool


def get_agent_registry() -> AgentRegistry:
    return get_agent_pool().registry


def _context_for_notification(
    base: AgentRuntimeContext,
    notification: AgentNotificationSnapshot,
) -> AgentRuntimeContext:
    return AgentRuntimeContext(
        session_id=base.session_id,
        user=base.user,
        agent_code=notification.target_agent_code,
        agent_instance_id=notification.target_agent_instance_id,
        nested_for_agent_code=notification.nested_for_agent_code,
        nested_call_id=notification.nested_call_id,
        knowledge_generation=base.knowledge_generation,
        sandbox_container_id=notification.sandbox_container_id,
        sandbox_container_generation=notification.sandbox_container_generation,
        sandbox_skill_metadata=notification.sandbox_skill_metadata,
        work_project_id=base.work_project_id,
    )


def _tag_notification_event(event: AgentEventSchema, context: AgentRuntimeContext) -> AgentEventSchema:
    if not context.nested_for_agent_code or not hasattr(event, "nested_for"):
        return event
    return event.model_copy(update={
        "nested_for": context.nested_for_agent_code,
        "nested_call_id": context.nested_call_id,
    })


def _next_turn_scope(context: AgentRuntimeContext) -> str:
    owner = context.agent_instance_id or main_agent_instance_id(
        context.session_id,
        context.user.id,
        context.agent_code,
    )
    return next_segment_scope(owner)


async def _mark_session_running(
    session_id: str,
    *,
    agent_code: str,
    sandbox_container_id: int | None,
    sandbox_container_generation: int,
) -> None:
    from service.agent import sessions as agent_sessions

    await agent_sessions.mark_session_running(
        session_id,
        agent_code=agent_code,
        sandbox_container_id=sandbox_container_id,
        sandbox_container_generation=sandbox_container_generation,
    )


async def _mark_session_stopped(session_id: str, *, error: str = "") -> None:
    from service.agent import sessions as agent_sessions

    await agent_sessions.mark_session_stopped(session_id, error=error)


async def _force_mark_session_stopped(session_id: str, *, error: str = "") -> None:
    from service.agent import sessions as agent_sessions

    await agent_sessions.force_mark_session_stopped(session_id, error=error)


async def _has_active_session_runtime(session_id: str) -> bool:
    from service.agent import sessions as agent_sessions

    return await agent_sessions.has_active_session_runtime(session_id)


async def _mark_sessions_stopped(session_ids: list[str], *, error: str = "") -> None:
    from service.agent import sessions as agent_sessions

    await agent_sessions.mark_sessions_stopped(session_ids, error=error)
