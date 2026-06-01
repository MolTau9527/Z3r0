import json
from datetime import datetime
from uuid import uuid4

from sqlalchemy import delete, exists, func, text
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from core.agent.constants import DEFAULT_AGENT_CODE
from core.delegation.subagents import cancel_session_subagent_runs
from core.runtime.events import event_from_subagent_task, events_from_sdk_message
from core.runtime.session import get_agent_pool, get_agent_registry
from core.conversation.store import StoredItem, fetch_stored_items
from core.sandbox.command_jobs import cancel_session_async_sandbox_commands
from database import get_async_session
from logger import get_logger
from model.agent.sessions import AgentSessionMeta
from model.agent.subordinates import AgentSubordinateTask
from model.work_project.projects import WorkProject, WorkProjectOwner
from schema.agent.events import AgentContentEventSchema
from schema.agent.sessions import AgentSessionSummarySchema, SessionType
from schema.system_user.users import SystemUserRole
from service.agent import notifications as agent_notifications
from service.agent import subordinates as agent_subordinates
from service.sandbox import async_jobs as sandbox_async_jobs
from utils.sdk_tables import BOOTSTRAP_SESSION_ID, agent_messages, agent_sessions


logger = get_logger(__name__)

_TITLE_MAX_LEN = 80
DEFAULT_REPLAY_EVENT_PAGE_SIZE = 80


async def create_session(user_id: int) -> str:
    session_id = str(uuid4())
    async with get_async_session() as session:
        await ensure_sdk_session_row(session, session_id)
        session.add(AgentSessionMeta(
            session_id=session_id,
            session_type=SessionType.CHAT,
            agent_code=DEFAULT_AGENT_CODE,
            owner_id=user_id,
        ))
        await session.commit()
    return session_id


async def update_session_title(
    session_id: str,
    title: str,
    user_id: int,
    user_role: SystemUserRole,
) -> AgentSessionSummarySchema | None:
    async with get_async_session() as session:
        meta = await session.get(AgentSessionMeta, session_id)
        if meta is None or not await _can_access_meta(session, meta, user_id, user_role):
            return None
        meta.title = title
        session.add(meta)
        await session.commit()
    return await session_summary(session_id, user_id=user_id, user_role=user_role)


async def ensure_chat_session_meta(
    session_id: str,
    user_text: str,
    requested_agent_code: str | None,
    user_id: int,
    user_role: SystemUserRole,
) -> str:
    # resolution: override > sticky > default
    valid = set(get_agent_registry().codes())
    override = requested_agent_code if requested_agent_code in valid else None

    async with get_async_session() as session:
        meta = await session.get(AgentSessionMeta, session_id)
        if meta is None or not await _can_access_meta(session, meta, user_id, user_role):
            raise PermissionError("agent session not found")
        existing = meta.agent_code if meta and meta.agent_code in valid else None
        resolved = override or existing or DEFAULT_AGENT_CODE

        if meta.agent_code != resolved:
            meta.agent_code = resolved
            if not meta.title:
                meta.title = _truncate(user_text)
            session.add(meta)
        elif not meta.title:
            meta.title = _truncate(user_text)
            session.add(meta)
        await session.commit()

    return resolved


async def list_sessions(
    limit: int = 100,
    user_id: int = 0,
    user_role: SystemUserRole = SystemUserRole.USER,
    project_id: int | None = None,
) -> list[AgentSessionSummarySchema]:
    return await _list_sessions(
        limit=limit,
        user_id=user_id,
        user_role=user_role,
        project_id=project_id,
    )


async def session_summary(
    session_id: str,
    user_id: int,
    user_role: SystemUserRole,
) -> AgentSessionSummarySchema | None:
    async with get_async_session() as session:
        if not await _can_access_session(session, session_id, user_id, user_role):
            return None
    return await _session_summary_by_id(session_id)


async def _list_sessions(
    limit: int,
    user_id: int,
    user_role: SystemUserRole,
    project_id: int | None = None,
) -> list[AgentSessionSummarySchema]:
    meta_table = AgentSessionMeta.__table__
    source = agent_sessions.join(
        meta_table,
        agent_sessions.c.session_id == meta_table.c.session_id,
    ).outerjoin(
        agent_messages,
        agent_sessions.c.session_id == agent_messages.c.session_id,
    )

    stmt = (
        select(
            agent_sessions.c.session_id,
            agent_sessions.c.created_at,
            agent_sessions.c.updated_at,
            func.count(agent_messages.c.id).label("message_count"),
        )
        .select_from(source)
        .where(agent_sessions.c.session_id != BOOTSTRAP_SESSION_ID)
        .group_by(
            agent_sessions.c.session_id,
            agent_sessions.c.created_at,
            agent_sessions.c.updated_at,
        )
        .order_by(agent_sessions.c.updated_at.desc())
        .limit(limit)
    )
    if project_id is None:
        stmt = stmt.where(
            meta_table.c.project_id.is_(None),
            meta_table.c.owner_id == user_id,
        )
    else:
        stmt = stmt.where(meta_table.c.project_id == project_id)
        if user_role != SystemUserRole.ADMIN:
            stmt = stmt.where(
                exists()
                .where(WorkProjectOwner.project_id == project_id)
                .where(WorkProjectOwner.user_id == user_id)
            )

    async with get_async_session() as session:
        rows = (await session.execute(stmt)).all()
        if not rows:
            return []

        session_ids = [row.session_id for row in rows]
        metas = {meta.session_id: meta for meta in (await session.exec(
            select(AgentSessionMeta).where(AgentSessionMeta.session_id.in_(session_ids))
        )).all()}

    return [_summary_from_row(row, metas.get(row.session_id)) for row in rows]


async def _session_summary_by_id(session_id: str) -> AgentSessionSummarySchema | None:
    meta_table = AgentSessionMeta.__table__
    source = agent_sessions.join(
        meta_table,
        agent_sessions.c.session_id == meta_table.c.session_id,
    ).outerjoin(
        agent_messages,
        agent_sessions.c.session_id == agent_messages.c.session_id,
    )
    stmt = (
        select(
            agent_sessions.c.session_id,
            agent_sessions.c.created_at,
            agent_sessions.c.updated_at,
            func.count(agent_messages.c.id).label("message_count"),
        )
        .select_from(source)
        .where(agent_sessions.c.session_id == session_id)
        .group_by(
            agent_sessions.c.session_id,
            agent_sessions.c.created_at,
            agent_sessions.c.updated_at,
        )
    )
    async with get_async_session() as session:
        row = (await session.execute(stmt)).first()
        if row is None:
            return None
        meta = await session.get(AgentSessionMeta, session_id)
    return _summary_from_row(row, meta)


def _summary_from_row(row, meta: AgentSessionMeta | None) -> AgentSessionSummarySchema:
    session_type = meta.session_type if meta else SessionType.CHAT
    return AgentSessionSummarySchema(
        session_id=row.session_id,
        session_type=session_type,
        title=_resolve_title(meta),
        agent_code=meta.agent_code if meta else "",
        owner_id=meta.owner_id if meta else 0,
        project_id=meta.project_id if meta else None,
        is_running=meta.is_running if meta else False,
        runtime_agent_code=meta.runtime_agent_code if meta else "",
        runtime_sandbox_container_id=meta.runtime_sandbox_container_id if meta else None,
        runtime_sandbox_container_generation=meta.runtime_sandbox_container_generation if meta else 0,
        run_started_at=meta.run_started_at if meta else None,
        run_finished_at=meta.run_finished_at if meta else None,
        run_error=meta.run_error if meta else "",
        message_count=row.message_count or 0,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


async def list_running_sessions() -> list[AgentSessionMeta]:
    async with get_async_session() as session:
        return list((await session.exec(
            select(AgentSessionMeta).where(AgentSessionMeta.is_running == True)  # noqa: E712
        )).all())


async def mark_session_running(
    session_id: str,
    *,
    agent_code: str,
    sandbox_container_id: int | None,
    sandbox_container_generation: int,
) -> None:
    async with get_async_session() as session:
        meta = await session.get(AgentSessionMeta, session_id)
        if meta is None:
            return
        meta.is_running = True
        meta.runtime_agent_code = agent_code
        meta.runtime_sandbox_container_id = sandbox_container_id
        meta.runtime_sandbox_container_generation = sandbox_container_generation
        meta.run_started_at = datetime.now()
        meta.run_finished_at = None
        meta.run_error = ""
        session.add(meta)
        await session.commit()


async def mark_session_stopped(session_id: str, *, error: str = "") -> None:
    if await has_active_session_runtime(session_id):
        return
    await finish_session_run(session_id, error=error)


async def finish_session_run(session_id: str, *, error: str = "") -> None:
    async with get_async_session() as session:
        meta = await session.get(AgentSessionMeta, session_id)
        if meta is None:
            return
        meta.is_running = False
        meta.run_finished_at = datetime.now()
        meta.run_error = _truncate_error(error)
        session.add(meta)
        await session.commit()


async def mark_sessions_stopped(session_ids: list[str], *, error: str = "") -> None:
    if not session_ids:
        return
    active_session_ids = {
        session_id for session_id in session_ids
        if await has_active_session_runtime(session_id)
    }
    async with get_async_session() as session:
        metas = (await session.exec(
            select(AgentSessionMeta).where(AgentSessionMeta.session_id.in_(session_ids))
        )).all()
        for meta in metas:
            if meta.session_id in active_session_ids:
                continue
            meta.is_running = False
            meta.run_finished_at = datetime.now()
            meta.run_error = _truncate_error(error)
            session.add(meta)
        await session.commit()


async def force_mark_session_stopped(session_id: str, *, error: str = "") -> None:
    async with get_async_session() as session:
        meta = await session.get(AgentSessionMeta, session_id)
        if meta is None:
            return
        meta.is_running = False
        meta.run_finished_at = datetime.now()
        meta.run_error = _truncate_error(error)
        session.add(meta)
        await session.commit()


async def has_active_session_runtime(session_id: str) -> bool:
    return (
        await agent_subordinates.has_running_subagent_tasks(session_id=session_id)
        or await agent_notifications.has_active_session_notifications(session_id=session_id)
        or await sandbox_async_jobs.has_running_async_jobs(session_id=session_id)
    )


async def replay_session_events(
    session_id: str,
    user_id: int,
    user_role: SystemUserRole,
) -> tuple[list[AgentContentEventSchema], bool, int | None] | None:
    return await replay_session_events_page(
        session_id=session_id,
        user_id=user_id,
        user_role=user_role,
        before_id=None,
        limit=DEFAULT_REPLAY_EVENT_PAGE_SIZE,
    )


async def replay_session_events_page(
    session_id: str,
    user_id: int,
    user_role: SystemUserRole,
    *,
    before_id: int | None,
    limit: int,
) -> tuple[list[AgentContentEventSchema], bool, int | None] | None:
    # nested-call items are tagged so the UI re-attaches them to the parent ToolCard
    limit = max(1, limit)
    fetch_limit = limit + 1
    async with get_async_session() as session:
        if not await _can_access_session(session, session_id, user_id, user_role):
            return None
        stored_items, has_more, next_before_id = await _fetch_replay_turn_page(
            session,
            session_id,
            before_id=before_id,
            limit=limit,
            fetch_limit=fetch_limit,
        )
        # On the initial load (before_id is None) the current turn alone is
        # often too narrow for a useful first paint — a single user message
        # plus one tool_call that delegates to a subagent currently emitting
        # hundreds of nested items will leave the visible scroll buffer
        # showing only "the last few messages" until the user manually
        # scrolls up to fetch more. Keep prepending whole prior turns until
        # the first paint covers a normal page worth of stored items (or we
        # reach the start of the session). Each prepended turn is still
        # turn-aligned so user/assistant boundaries stay clean in the UI.
        if before_id is None:
            while has_more and stored_items and len(stored_items) < limit:
                prior_items, prior_has_more, prior_next_before_id = await _fetch_replay_turn_page(
                    session,
                    session_id,
                    before_id=stored_items[0].message_id,
                    limit=limit,
                    fetch_limit=fetch_limit,
                )
                if not prior_items:
                    break
                stored_items = prior_items + stored_items
                has_more = prior_has_more
                next_before_id = prior_next_before_id if prior_has_more else None
        sub_tasks = list((await session.exec(
            select(AgentSubordinateTask)
            .where(AgentSubordinateTask.session_id == session_id)
            .order_by(AgentSubordinateTask.created_at)
        )).all())

    code_to_name = get_agent_registry().code_to_name()
    top_level_events: list[AgentContentEventSchema] = []
    nested_events_by_call_id: dict[str, list[AgentContentEventSchema]] = {}
    for stored in stored_items:
        agent_name = code_to_name.get(stored.owner_code, "")
        for event in events_from_sdk_message(
            stored.item, str(stored.message_id),
            created_at=stored.created_at,
            owner_code=stored.owner_code,
            agent_name=agent_name,
            nested_for=stored.nested_for,
            nested_call_id=stored.nested_call_id,
        ):
            nested_call_id = getattr(event, "nested_call_id", "")
            if nested_call_id:
                nested_events_by_call_id.setdefault(nested_call_id, []).append(event)
            else:
                top_level_events.append(event)

    task_events_by_call_id: dict[str, list[AgentContentEventSchema]] = {}
    for task in sub_tasks:
        if not task.nested_call_id:
            continue
        task_events_by_call_id.setdefault(task.nested_call_id, []).append(event_from_subagent_task(
            run_id=task.run_id,
            parent_agent_code=task.parent_agent_code,
            parent_agent_instance_id=task.parent_agent_instance_id,
            agent_code=task.agent_code,
            agent_name=task.agent_name,
            status=task.status,
            result=task.result,
            error=task.error,
            progress=task.progress,
            nested_call_id=task.nested_call_id,
            created_at=task.updated_at,
        ))

    events = _normalize_replay_events(_attach_nested_replay_events(
        top_level_events,
        nested_events_by_call_id,
        task_events_by_call_id,
        include_orphans=not has_more,
    ))
    return events, has_more, next_before_id


async def _fetch_replay_turn_page(
    session: AsyncSession,
    session_id: str,
    *,
    before_id: int | None,
    limit: int,
    fetch_limit: int,
) -> tuple[list[StoredItem], bool, int | None]:
    """Fetch one conversation turn ending before *before_id* (latest turn if None).

    A "turn" starts at the most recent top-level user message that fits the
    window.  The page extends backwards across sibling fetches until either a
    user-message boundary is found or history is exhausted.
    """
    window = await fetch_stored_items(
        session,
        session_id,
        before_id=before_id,
        limit=fetch_limit,
    )
    has_more = len(window) > limit
    if not has_more:
        return window, False, None

    window = window[-limit:]
    while True:
        turn_start = _top_level_user_index(window)
        if turn_start != -1:
            page = window[turn_start:]
            return page, True, page[0].message_id
        older = await fetch_stored_items(
            session,
            session_id,
            before_id=window[0].message_id,
            limit=fetch_limit,
        )
        if not older:
            return window, False, None
        older_has_more = len(older) > limit
        window = (older[-limit:] if older_has_more else older) + window
        if not older_has_more:
            # End of history reached without a user-message boundary; keep
            # whatever leading items remain so nothing is silently dropped.
            return window, False, None


def _attach_nested_replay_events(
    top_level_events: list[AgentContentEventSchema],
    nested_events_by_call_id: dict[str, list[AgentContentEventSchema]],
    task_events_by_call_id: dict[str, list[AgentContentEventSchema]],
    *,
    include_orphans: bool = True,
) -> list[AgentContentEventSchema]:
    events: list[AgentContentEventSchema] = []
    for event in top_level_events:
        events.append(event)
        if getattr(event, "type", "") != "tool_call":
            continue
        call_id = getattr(event, "call_id", "")
        if not call_id:
            continue
        events.extend(task_events_by_call_id.pop(call_id, ()))
        events.extend(nested_events_by_call_id.pop(call_id, ()))
    if include_orphans:
        for call_id in sorted(set(task_events_by_call_id) | set(nested_events_by_call_id)):
            events.extend(task_events_by_call_id.get(call_id, ()))
            events.extend(nested_events_by_call_id.get(call_id, ()))
    return events


def _top_level_user_index(stored_items: list[StoredItem]) -> int:
    for index, stored in enumerate(stored_items):
        if _is_top_level_user_item(stored):
            return index
    return -1


def _is_top_level_user_item(stored: StoredItem) -> bool:
    if stored.nested_call_id:
        return False
    return stored.item.get("role") == "user"


def _normalize_replay_events(events: list[AgentContentEventSchema]) -> list[AgentContentEventSchema]:
    """Collapse duplicate persisted facts before the UI rebuilds transcript state."""
    return _ReplayEventNormalizer().normalize(events)


class _ReplayEventNormalizer:
    def __init__(self) -> None:
        self.normalized: list[AgentContentEventSchema] = []
        self.turn_seen_text: set[tuple[str, str, str, str]] = set()
        self.tool_indexes: dict[tuple[str, str, str], int] = {}
        self.tool_result_indexes: dict[tuple[str, str, str], int] = {}
        self.tool_semantic_indexes: dict[tuple[str, str, str, str], int] = {}
        self.tool_call_aliases: dict[tuple[str, str, str], str] = {}
        self.subagent_indexes: dict[str, int] = {}

    def normalize(self, events: list[AgentContentEventSchema]) -> list[AgentContentEventSchema]:
        for event in events:
            self._append(event)
        return self.normalized

    def _append(self, event: AgentContentEventSchema) -> None:
        event = _normalize_tool_call_id(event, self.tool_call_aliases)
        event_type = str(getattr(event, "type", ""))
        if event_type in {"user_message", "turn_boundary"} and not getattr(event, "nested_call_id", ""):
            self._reset_turn()
            self.normalized.append(event)
            return
        if event_type in {"text_complete", "thinking_complete"}:
            self._append_text_complete(event, event_type)
            return
        if event_type == "tool_call":
            self._append_tool_call(event)
            return
        if event_type == "tool_result":
            self._append_tool_result(event)
            return
        if event_type == "subagent_task":
            self._append_subagent_task(event)
            return
        self.normalized.append(event)

    def _reset_turn(self) -> None:
        self.turn_seen_text.clear()
        self.tool_indexes.clear()
        self.tool_result_indexes.clear()
        self.tool_semantic_indexes.clear()
        self.tool_call_aliases.clear()
        self.subagent_indexes.clear()

    def _append_text_complete(self, event: AgentContentEventSchema, event_type: str) -> None:
        key = (
            event_type,
            getattr(event, "nested_for", ""),
            getattr(event, "nested_call_id", ""),
            getattr(event, "text", ""),
        )
        if key in self.turn_seen_text:
            return
        self.turn_seen_text.add(key)
        self.normalized.append(event)

    def _append_tool_call(self, event: AgentContentEventSchema) -> None:
        key = _tool_event_key(event)
        semantic_key = _tool_semantic_key(event)
        existing_index = self.tool_indexes.get(key)
        if existing_index is None:
            existing_index = self.tool_semantic_indexes.get(semantic_key)
        if existing_index is None:
            self.tool_indexes[key] = len(self.normalized)
            self.tool_semantic_indexes[semantic_key] = len(self.normalized)
            self.normalized.append(event)
            return

        existing = self.normalized[existing_index]
        existing_call_id = getattr(existing, "call_id", "")
        incoming_call_id = getattr(event, "call_id", "")
        if existing_call_id and incoming_call_id and existing_call_id != incoming_call_id:
            self.tool_call_aliases[key] = existing_call_id
        self.normalized[existing_index] = _merge_tool_call(existing, event, call_id=existing_call_id)

    def _append_tool_result(self, event: AgentContentEventSchema) -> None:
        key = _tool_event_key(event)
        existing_index = self.tool_result_indexes.get(key)
        if existing_index is None:
            self.tool_result_indexes[key] = len(self.normalized)
            self.normalized.append(event)
            return
        self.normalized[existing_index] = event

    def _append_subagent_task(self, event: AgentContentEventSchema) -> None:
        run_id = getattr(event, "run_id", "")
        existing_index = self.subagent_indexes.get(run_id)
        if run_id and existing_index is not None:
            self.normalized[existing_index] = event
            return
        if run_id:
            self.subagent_indexes[run_id] = len(self.normalized)
        self.normalized.append(event)


def _merge_tool_call(
    existing: AgentContentEventSchema,
    incoming: AgentContentEventSchema,
    *,
    call_id: str = "",
) -> AgentContentEventSchema:
    if getattr(incoming, "name", "") or getattr(incoming, "arguments", None):
        if call_id and getattr(incoming, "call_id", "") != call_id:
            return incoming.model_copy(update={"call_id": call_id})
        return incoming
    return existing


def _normalize_tool_call_id(
    event: AgentContentEventSchema,
    aliases: dict[tuple[str, str, str], str],
) -> AgentContentEventSchema:
    event_type = str(getattr(event, "type", ""))
    if event_type not in {"tool_call", "tool_result"}:
        return event
    key = _tool_event_key(event)
    alias = aliases.get(key)
    if not alias or getattr(event, "call_id", "") == alias:
        return event
    return event.model_copy(update={"call_id": alias})


def _tool_event_key(event: AgentContentEventSchema) -> tuple[str, str, str]:
    return (
        getattr(event, "nested_for", ""),
        getattr(event, "nested_call_id", ""),
        getattr(event, "call_id", ""),
    )


def _tool_semantic_key(event: AgentContentEventSchema) -> tuple[str, str, str, str]:
    return (
        getattr(event, "nested_for", ""),
        getattr(event, "nested_call_id", ""),
        getattr(event, "name", ""),
        _stable_json(getattr(event, "arguments", None)),
    )


def _stable_json(value) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    except TypeError:
        return str(value)


async def can_access_session(session_id: str, user_id: int, user_role: SystemUserRole) -> bool:
    async with get_async_session() as session:
        return await _can_access_session(session, session_id, user_id, user_role)


async def get_session_meta(session_id: str) -> AgentSessionMeta | None:
    async with get_async_session() as session:
        return await session.get(AgentSessionMeta, session_id)


async def project_id_for_session(session_id: str) -> int | None:
    meta = await get_session_meta(session_id)
    return meta.project_id if meta is not None else None


async def delete_session(
    session_id: str,
    user_id: int = 0,
    user_role: SystemUserRole = SystemUserRole.USER,
    *,
    allow_project_session: bool = False,
) -> bool:
    if not session_id:
        return False

    async with get_async_session() as session:
        meta = await session.get(AgentSessionMeta, session_id)
        if meta is None or not await _can_access_meta(session, meta, user_id, user_role):
            return False
        if meta.project_id is not None and not allow_project_session:
            return False

    await cancel_session_subagent_runs(session_id)
    await cancel_session_async_sandbox_commands(session_id)
    await get_agent_pool().discard(session_id)

    async with get_async_session() as session:
        records_deleted = await _delete_session_records_in_tx(session, session_id)
        await session.commit()

    if records_deleted:
        logger.info("agent session deleted: %s", session_id)
    return records_deleted


async def cancel_sessions(session_ids: list[str], reason: str) -> None:
    for session_id in session_ids:
        await get_agent_pool().cancel_all(session_id)
    await mark_sessions_stopped(session_ids, error=reason)


async def _delete_session_records_in_tx(session: AsyncSession, session_id: str) -> bool:
    # one DELETE drops the SDK session row and the FK CASCADE chain takes
    # care of agent_messages, agent_message_meta, and agent_session_meta
    result = await session.execute(
        delete(agent_sessions).where(agent_sessions.c.session_id == session_id)
    )
    return (result.rowcount or 0) > 0


async def ensure_sdk_session_row(session: AsyncSession, session_id: str) -> None:
    # placeholder row owned by the SDK; required so AgentSessionMeta's FK can
    # bind and so list_sessions can surface freshly-created empty conversations
    await session.execute(
        text(
            "INSERT INTO agent_sessions (session_id) VALUES (:sid) "
            "ON CONFLICT (session_id) DO NOTHING"
        ),
        {"sid": session_id},
    )


async def _can_access_session(
    session: AsyncSession,
    session_id: str,
    user_id: int,
    user_role: SystemUserRole,
) -> bool:
    meta = await session.get(AgentSessionMeta, session_id)
    return meta is not None and await _can_access_meta(session, meta, user_id, user_role)


async def _can_access_meta(
    session: AsyncSession,
    meta: AgentSessionMeta,
    user_id: int,
    user_role: SystemUserRole,
) -> bool:
    if meta.project_id is None:
        return meta.owner_id == user_id
    if await session.get(WorkProject, meta.project_id) is None:
        return False
    if user_role == SystemUserRole.ADMIN:
        return True
    return await session.get(WorkProjectOwner, (meta.project_id, user_id)) is not None


def _resolve_title(meta: AgentSessionMeta | None) -> str:
    if meta is None:
        return ""
    return meta.title or ("Project session" if meta.session_type == SessionType.PROJECT else "Untitled session")


def _truncate(value: str) -> str:
    value = value.strip().replace("\n", " ")
    return value if len(value) <= _TITLE_MAX_LEN else value[: _TITLE_MAX_LEN - 1] + "..."


def _truncate_error(value: str) -> str:
    value = value.strip().replace("\n", " ")
    return value if len(value) <= 500 else value[:499] + "..."
