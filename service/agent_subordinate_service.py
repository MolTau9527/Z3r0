from datetime import datetime
from uuid import uuid4

from sqlmodel import select

from database import get_async_session
from logger import get_logger
from model.agent_subordinate_model import AgentSubordinateTask
from schema.agent_subordinate_schema import AgentSubordinateStatus, AgentSubordinateTaskSnapshot
from schema.system_user_schema import SystemUserRole


logger = get_logger(__name__)


TERMINAL_SUBAGENT_STATUSES = {
    AgentSubordinateStatus.COMPLETED,
    AgentSubordinateStatus.FAILED,
    AgentSubordinateStatus.CANCELED,
}


async def create_subagent_task(
    *,
    session_id: str,
    parent_agent_code: str,
    agent_code: str,
    agent_name: str,
    brief: str,
    nested_call_id: str,
    owner_id: int,
) -> AgentSubordinateTaskSnapshot:
    now = datetime.now()
    task = AgentSubordinateTask(
        run_id=str(uuid4()),
        session_id=session_id,
        parent_agent_code=parent_agent_code,
        agent_code=agent_code,
        agent_name=agent_name,
        status=AgentSubordinateStatus.RUNNING.value,
        brief=brief,
        nested_call_id=nested_call_id,
        owner_id=owner_id,
        created_at=now,
        updated_at=now,
        started_at=now,
    )
    async with get_async_session() as session:
        session.add(task)
        await session.commit()
        await session.refresh(task)
    logger.info("subagent task created: %s", task.run_id)
    return snapshot_from_task(task)


async def get_subagent_task(
    *,
    run_id: str,
    session_id: str,
    user_id: int,
    user_role: SystemUserRole,
) -> AgentSubordinateTaskSnapshot | None:
    async with get_async_session() as session:
        task = await session.get(AgentSubordinateTask, run_id)
        if task is None or not _can_access_task(task, session_id, user_id, user_role):
            return None
        return snapshot_from_task(task)


async def get_subagent_task_internal(run_id: str) -> AgentSubordinateTaskSnapshot | None:
    async with get_async_session() as session:
        task = await session.get(AgentSubordinateTask, run_id)
        return snapshot_from_task(task) if task is not None else None


async def update_subagent_progress(run_id: str, progress: str) -> AgentSubordinateTaskSnapshot | None:
    async with get_async_session() as session:
        task = await session.get(AgentSubordinateTask, run_id)
        if task is None or task.status in TERMINAL_SUBAGENT_STATUSES:
            return None
        task.progress = progress
        task.updated_at = datetime.now()
        session.add(task)
        await session.commit()
        await session.refresh(task)
        return snapshot_from_task(task)


async def complete_subagent_task(run_id: str, result: str) -> AgentSubordinateTaskSnapshot | None:
    return await _finish_subagent_task(run_id, AgentSubordinateStatus.COMPLETED, result=result)


async def fail_subagent_task(run_id: str, error: str) -> AgentSubordinateTaskSnapshot | None:
    return await _finish_subagent_task(run_id, AgentSubordinateStatus.FAILED, error=error)


async def cancel_subagent_task_record(run_id: str, error: str = "") -> AgentSubordinateTaskSnapshot | None:
    return await _finish_subagent_task(run_id, AgentSubordinateStatus.CANCELED, error=error)


async def mark_stale_running_subagent_tasks_failed() -> int:
    now = datetime.now()
    async with get_async_session() as session:
        rows = (await session.exec(
            select(AgentSubordinateTask).where(AgentSubordinateTask.status == AgentSubordinateStatus.RUNNING.value)
        )).all()
        for task in rows:
            task.status = AgentSubordinateStatus.FAILED.value
            task.error = "Subagent task was interrupted by backend restart."
            task.updated_at = now
            task.finished_at = now
            session.add(task)
        if rows:
            await session.commit()
    if rows:
        logger.info("stale subagent tasks marked failed: %d", len(rows))
    return len(rows)


async def _finish_subagent_task(
    run_id: str,
    status: AgentSubordinateStatus,
    *,
    result: str = "",
    error: str = "",
) -> AgentSubordinateTaskSnapshot | None:
    now = datetime.now()
    async with get_async_session() as session:
        task = await session.get(AgentSubordinateTask, run_id)
        if task is None:
            return None
        if task.status in TERMINAL_SUBAGENT_STATUSES:
            return snapshot_from_task(task)
        task.status = status.value
        task.result = result
        task.error = error
        task.progress = ""
        task.updated_at = now
        task.finished_at = now
        session.add(task)
        await session.commit()
        await session.refresh(task)
        return snapshot_from_task(task)


def snapshot_from_task(task: AgentSubordinateTask) -> AgentSubordinateTaskSnapshot:
    return AgentSubordinateTaskSnapshot(
        run_id=task.run_id,
        session_id=task.session_id,
        parent_agent_code=task.parent_agent_code,
        agent_code=task.agent_code,
        agent_name=task.agent_name,
        status=_coerce_subagent_status(task.status),
        brief=task.brief,
        result=task.result,
        error=task.error,
        progress=task.progress,
        nested_call_id=task.nested_call_id,
        created_at=task.created_at,
        updated_at=task.updated_at,
        started_at=task.started_at,
        finished_at=task.finished_at,
    )


def _coerce_subagent_status(status: AgentSubordinateStatus | str) -> AgentSubordinateStatus:
    if isinstance(status, AgentSubordinateStatus):
        return status
    return AgentSubordinateStatus(status.lower())


def _can_access_task(
    task: AgentSubordinateTask,
    session_id: str,
    user_id: int,
    user_role: SystemUserRole,
) -> bool:
    if task.session_id != session_id:
        return False
    return user_role == SystemUserRole.ADMIN or task.owner_id == user_id
