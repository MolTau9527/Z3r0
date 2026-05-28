from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Any
from uuid import uuid4

from sqlalchemy.exc import IntegrityError
from sqlmodel import select, update

from core.runtime.context import MAIN_AGENT_INSTANCE_PREFIX
from database import get_async_session
from logger import get_logger
from model.agent.notifications import AgentNotification
from schema.agent.notifications import (
    AgentNotificationKind,
    AgentNotificationSnapshot,
    AgentNotificationStatus,
)
from schema.agent.subordinates import AgentSubordinateTaskSnapshot
from schema.sandbox.async_jobs import SandboxAsyncJobSnapshot


logger = get_logger(__name__)
_notification_claim_lock = asyncio.Lock()
_TERMINAL_NOTIFICATION_STATUSES = {
    AgentNotificationStatus.COMPLETED,
    AgentNotificationStatus.FAILED,
    AgentNotificationStatus.CANCELED,
}

_MAIN_AGENT_TARGET = f"{MAIN_AGENT_INSTANCE_PREFIX}%"


async def enqueue_subagent_finished_notification(
    snapshot: AgentSubordinateTaskSnapshot,
    *,
    sandbox_container_id: int | None = None,
    sandbox_container_generation: int = 0,
    sandbox_skill_metadata: tuple[str, ...] = (),
) -> AgentNotificationSnapshot | None:
    """Create one durable inbox item for the parent agent of a terminal subagent run."""

    payload = {
        "run_id": snapshot.run_id,
        "agent_code": snapshot.agent_code,
        "agent_name": snapshot.agent_name,
        "status": snapshot.status.value,
        "result": snapshot.result,
        "error": snapshot.error,
    }
    notification = AgentNotification(
        id=str(uuid4()),
        session_id=snapshot.session_id,
        target_agent_code=snapshot.parent_agent_code,
        target_agent_instance_id=snapshot.parent_agent_instance_id,
        nested_for_agent_code="",
        nested_call_id="",
        sandbox_container_id=sandbox_container_id,
        sandbox_container_generation=sandbox_container_generation,
        sandbox_skill_metadata=list(sandbox_skill_metadata),
        kind=AgentNotificationKind.SUBAGENT_FINISHED.value,
        status=AgentNotificationStatus.PENDING.value,
        run_id=snapshot.run_id,
        payload=payload,
    )
    async with get_async_session() as session:
        try:
            session.add(notification)
            await session.commit()
            await session.refresh(notification)
        except IntegrityError:
            await session.rollback()
            existing = (await session.exec(
                select(AgentNotification).where(
                    AgentNotification.kind == AgentNotificationKind.SUBAGENT_FINISHED.value,
                    AgentNotification.run_id == snapshot.run_id,
                )
            )).first()
            return snapshot_from_notification(existing) if existing is not None else None

    logger.debug(
        "agent notification queued: %s session=%s run=%s target=%s",
        notification.id,
        notification.session_id,
        notification.run_id,
        notification.target_agent_code,
    )
    return snapshot_from_notification(notification)


async def enqueue_sandbox_async_job_finished_notification(
    snapshot: SandboxAsyncJobSnapshot,
) -> AgentNotificationSnapshot | None:
    payload = {
        "status": snapshot.status.value,
        "output_file": snapshot.output_file,
        "output_bytes": snapshot.output_bytes,
        "output_lines": snapshot.output_lines,
        "exit_code": snapshot.exit_code,
        "error": snapshot.error,
    }
    notification = AgentNotification(
        id=str(uuid4()),
        session_id=snapshot.session_id,
        target_agent_code=snapshot.agent_code,
        target_agent_instance_id=snapshot.agent_instance_id,
        nested_for_agent_code=snapshot.nested_for_agent_code,
        nested_call_id=snapshot.nested_call_id,
        sandbox_container_id=snapshot.sandbox_container_id,
        sandbox_container_generation=snapshot.sandbox_container_generation,
        sandbox_skill_metadata=list(snapshot.sandbox_skill_metadata),
        kind=AgentNotificationKind.SANDBOX_ASYNC_JOB_FINISHED.value,
        status=AgentNotificationStatus.PENDING.value,
        run_id=snapshot.run_id,
        payload=payload,
    )
    async with get_async_session() as session:
        try:
            session.add(notification)
            await session.commit()
            await session.refresh(notification)
        except IntegrityError:
            await session.rollback()
            existing = (await session.exec(
                select(AgentNotification).where(
                    AgentNotification.kind == AgentNotificationKind.SANDBOX_ASYNC_JOB_FINISHED.value,
                    AgentNotification.run_id == snapshot.run_id,
                )
            )).first()
            return snapshot_from_notification(existing) if existing is not None else None
    logger.debug(
        "sandbox async job notification queued: %s session=%s run=%s target=%s",
        notification.id,
        notification.session_id,
        notification.run_id,
        notification.target_agent_code,
    )
    return snapshot_from_notification(notification)


async def claim_next_pending_notification(
    *,
    session_id: str,
    target_agent_code: str | None = None,
    target_agent_instance_id: str | None = None,
) -> AgentNotificationSnapshot | None:
    async with _notification_claim_lock:
        return await _claim_next_pending_notification_locked(
            session_id=session_id,
            target_agent_code=target_agent_code,
            target_agent_instance_id=target_agent_instance_id,
        )


async def _claim_next_pending_notification_locked(
    *,
    session_id: str,
    target_agent_code: str | None,
    target_agent_instance_id: str | None,
) -> AgentNotificationSnapshot | None:
    now = datetime.now()
    async with get_async_session() as session:
        statement = select(AgentNotification).where(
            AgentNotification.session_id == session_id,
            AgentNotification.status == AgentNotificationStatus.PENDING.value,
        )
        if target_agent_code is not None:
            statement = statement.where(AgentNotification.target_agent_code == target_agent_code)
        statement = _filter_notification_target(statement, target_agent_instance_id)
        notification = (await session.exec(
            statement
            .order_by(AgentNotification.created_at.asc())
            .limit(1)
            .with_for_update(skip_locked=True)
        )).first()
        if notification is None:
            return None
        notification_id = notification.id
        updated = await session.exec(
            update(AgentNotification)
            .where(
                AgentNotification.id == notification_id,
                AgentNotification.status == AgentNotificationStatus.PENDING.value,
            )
            .values(
                status=AgentNotificationStatus.PROCESSING.value,
                started_at=now,
                updated_at=now,
            )
        )
        if updated.rowcount != 1:
            await session.rollback()
            return None
        await session.commit()
        claimed = await session.get(AgentNotification, notification_id)
        return snapshot_from_notification(claimed) if claimed is not None else None


async def has_pending_notification(
    *,
    session_id: str,
    target_agent_code: str | None = None,
    target_agent_instance_id: str | None = None,
) -> bool:
    async with get_async_session() as session:
        statement = select(AgentNotification.id).where(
            AgentNotification.session_id == session_id,
            AgentNotification.status == AgentNotificationStatus.PENDING.value,
        )
        if target_agent_code is not None:
            statement = statement.where(AgentNotification.target_agent_code == target_agent_code)
        statement = _filter_notification_target(statement, target_agent_instance_id)
        notification_id = (await session.exec(
            statement
            .limit(1)
        )).first()
        return notification_id is not None


async def has_pending_main_agent_notification(*, session_id: str) -> bool:
    async with get_async_session() as session:
        notification_id = (await session.exec(
            select(AgentNotification.id)
            .where(
                AgentNotification.session_id == session_id,
                AgentNotification.status == AgentNotificationStatus.PENDING.value,
                AgentNotification.target_agent_instance_id.like(_MAIN_AGENT_TARGET),
            )
            .limit(1)
        )).first()
        return notification_id is not None


async def has_active_session_notifications(*, session_id: str) -> bool:
    async with get_async_session() as session:
        notification_id = (await session.exec(
            select(AgentNotification.id)
            .where(
                AgentNotification.session_id == session_id,
                AgentNotification.status.in_([
                    AgentNotificationStatus.PENDING.value,
                    AgentNotificationStatus.PROCESSING.value,
                ]),
            )
            .limit(1)
        )).first()
        return notification_id is not None


async def complete_notification(notification_id: str) -> AgentNotificationSnapshot | None:
    return await _finish_notification(notification_id, AgentNotificationStatus.COMPLETED)


async def fail_notification(notification_id: str, error: str) -> AgentNotificationSnapshot | None:
    return await _finish_notification(notification_id, AgentNotificationStatus.FAILED, error=error)


async def release_notification(notification_id: str) -> AgentNotificationSnapshot | None:
    now = datetime.now()
    async with get_async_session() as session:
        notification = await session.get(AgentNotification, notification_id)
        if notification is None:
            return None
        if notification.status != AgentNotificationStatus.PROCESSING.value:
            return snapshot_from_notification(notification)
        updated = await session.exec(
            update(AgentNotification)
            .where(
                AgentNotification.id == notification_id,
                AgentNotification.status == AgentNotificationStatus.PROCESSING.value,
            )
            .values(
                status=AgentNotificationStatus.PENDING.value,
                error="",
                started_at=None,
                updated_at=now,
            )
        )
        if updated.rowcount != 1:
            await session.rollback()
            current = await session.get(AgentNotification, notification_id)
            return snapshot_from_notification(current) if current is not None else None
        await session.commit()
        current = await session.get(AgentNotification, notification_id)
        return snapshot_from_notification(current) if current is not None else None


async def cancel_session_notifications(
    session_id: str,
    error: str = "",
    *,
    target_agent_instance_id: str | None = None,
) -> list[AgentNotificationSnapshot]:
    now = datetime.now()
    async with get_async_session() as session:
        statement = select(AgentNotification).where(
            AgentNotification.session_id == session_id,
            AgentNotification.status.in_([
                AgentNotificationStatus.PENDING.value,
                AgentNotificationStatus.PROCESSING.value,
            ]),
        )
        if target_agent_instance_id is not None:
            statement = statement.where(AgentNotification.target_agent_instance_id == target_agent_instance_id)
        rows = (await session.exec(statement)).all()
        for notification in rows:
            notification.status = AgentNotificationStatus.CANCELED.value
            notification.error = error
            notification.updated_at = now
            notification.finished_at = now
            session.add(notification)
        if not rows:
            return []
        await session.commit()
        for notification in rows:
            await session.refresh(notification)
        return [snapshot_from_notification(notification) for notification in rows]


async def reset_processing_notifications_all() -> int:
    now = datetime.now()
    async with get_async_session() as session:
        rows = (await session.exec(
            select(AgentNotification).where(
                AgentNotification.status == AgentNotificationStatus.PROCESSING.value,
            )
        )).all()
        for notification in rows:
            notification.status = AgentNotificationStatus.PENDING.value
            notification.error = ""
            notification.started_at = None
            notification.updated_at = now
            session.add(notification)
        if rows:
            await session.commit()
            logger.info("processing agent notifications reset: %d", len(rows))
        return len(rows)


async def _finish_notification(
    notification_id: str,
    status: AgentNotificationStatus,
    *,
    error: str = "",
) -> AgentNotificationSnapshot | None:
    now = datetime.now()
    async with get_async_session() as session:
        notification = await session.get(AgentNotification, notification_id)
        if notification is None:
            return None
        if _coerce_status(notification.status) in _TERMINAL_NOTIFICATION_STATUSES:
            return snapshot_from_notification(notification)
        updated = await session.exec(
            update(AgentNotification)
            .where(
                AgentNotification.id == notification_id,
                AgentNotification.status == AgentNotificationStatus.PROCESSING.value,
            )
            .values(
                status=status.value,
                error=error,
                updated_at=now,
                finished_at=now,
            )
        )
        if updated.rowcount != 1:
            await session.rollback()
            current = await session.get(AgentNotification, notification_id)
            return snapshot_from_notification(current) if current is not None else None
        await session.commit()
        current = await session.get(AgentNotification, notification_id)
        return snapshot_from_notification(current) if current is not None else None


def snapshot_from_notification(notification: AgentNotification) -> AgentNotificationSnapshot:
    payload = _coerce_payload(notification.payload)
    return AgentNotificationSnapshot(
        id=notification.id,
        session_id=notification.session_id,
        target_agent_code=notification.target_agent_code,
        target_agent_instance_id=notification.target_agent_instance_id,
        nested_for_agent_code=notification.nested_for_agent_code,
        nested_call_id=notification.nested_call_id,
        kind=_coerce_kind(notification.kind),
        status=_coerce_status(notification.status),
        run_id=notification.run_id,
        payload=payload,
        error=notification.error,
        sandbox_container_id=notification.sandbox_container_id,
        sandbox_container_generation=notification.sandbox_container_generation,
        sandbox_skill_metadata=_coerce_string_tuple(notification.sandbox_skill_metadata),
        created_at=notification.created_at,
        updated_at=notification.updated_at,
        started_at=notification.started_at,
        finished_at=notification.finished_at,
    )


def _coerce_kind(value: AgentNotificationKind | str) -> AgentNotificationKind:
    if isinstance(value, AgentNotificationKind):
        return value
    return AgentNotificationKind(str(value).lower())


def _coerce_status(value: AgentNotificationStatus | str) -> AgentNotificationStatus:
    if isinstance(value, AgentNotificationStatus):
        return value
    return AgentNotificationStatus(str(value).lower())


def _coerce_payload(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _coerce_string_tuple(value: Any) -> tuple[str, ...]:
    if not isinstance(value, list | tuple):
        return ()
    return tuple(item for item in value if isinstance(item, str))


def _filter_notification_target(statement: Any, target_agent_instance_id: str | None) -> Any:
    if target_agent_instance_id is not None:
        return statement.where(AgentNotification.target_agent_instance_id == target_agent_instance_id)
    return statement.where(AgentNotification.target_agent_instance_id.like(_MAIN_AGENT_TARGET))
