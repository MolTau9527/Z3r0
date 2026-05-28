import re
from datetime import datetime
from uuid import uuid4

from sqlalchemy import String, cast, func, or_
from sqlmodel import select

from core.agent.constants import DEFAULT_AGENT_CODE
from database import get_async_session
from logger import get_logger
from model.agent.sessions import AgentSessionMeta
from model.sandbox.containers import SandboxContainer
from model.system_user.users import SystemUser
from model.work_project.projects import WorkProject, WorkProjectOwner
from schema.agent.sessions import AgentSessionSummarySchema, SessionType
from schema.sandbox.containers import SandboxContainerStatus
from schema.system_user.users import SystemUserRole
from schema.work_project.projects import (
    CreateWorkProjectRequest,
    UpdateWorkProjectMetadataRequest,
    WorkProjectAgentSummarySchema,
    WorkProjectOwnerSchema,
    WorkProjectSchema,
    WorkProjectStatus,
    WorkProjectTaskSchema,
)
from service.agent.sessions import cancel_sessions, delete_session, ensure_sdk_session_row, list_sessions
from service.common.pagination import Page, paginate_statement
from service.work_project.progress import derive_work_project_status


logger = get_logger(__name__)
_PROJECT_SESSION_TITLE_PATTERN = re.compile(r"^session (?P<number>\d+)$")


class WorkProjectSessionCreateResult:
    def __init__(self, session_id: str = "", not_found: bool = False, inactive: bool = False) -> None:
        self.session_id = session_id
        self.not_found = not_found
        self.inactive = inactive


def can_create_work_project_session(status: WorkProjectStatus) -> bool:
    return status != WorkProjectStatus.CANCELED


def can_cancel_work_project(status: WorkProjectStatus) -> bool:
    return status != WorkProjectStatus.CANCELED


def can_retry_work_project(status: WorkProjectStatus) -> bool:
    return status == WorkProjectStatus.CANCELED


async def validate_work_project_metadata(
    request: CreateWorkProjectRequest | UpdateWorkProjectMetadataRequest,
) -> str:
    async with get_async_session() as session:
        if request.owner_user_ids:
            users = (await session.exec(
                select(SystemUser.id).where(SystemUser.id.in_(request.owner_user_ids))
            )).all()
            missing_owner_ids = sorted(set(request.owner_user_ids) - {user_id for user_id in users})
            if missing_owner_ids:
                return f"selected owners not found: {', '.join(str(id) for id in missing_owner_ids)}"

        if request.sandbox_container_id is not None:
            container = await session.get(SandboxContainer, request.sandbox_container_id)
            if container is None:
                return "selected sandbox container not found"
            if container.status != SandboxContainerStatus.RUNNING:
                return "selected sandbox container is not running"

    return ""


async def work_project_exists(id: int) -> bool:
    async with get_async_session() as session:
        return await session.get(WorkProject, id) is not None


async def create_work_project(request: CreateWorkProjectRequest) -> WorkProjectSchema:
    now = datetime.now()
    project = WorkProject(
        name=request.name,
        description=request.description,
        sandbox_container_id=request.sandbox_container_id,
        assets_text=request.assets_text,
        tasks=[],
        progress=0,
        status=WorkProjectStatus.WORKING,
        type=request.type,
        created_at=now,
        updated_at=now,
    )

    async with get_async_session() as session:
        session.add(project)
        await session.flush()
        _set_project_owner_rows(session, project.id or 0, request.owner_user_ids)
        await session.commit()
        await session.refresh(project)
        schema = await _project_schema(session, project)

    logger.info("work project created: %s", project.id)
    return schema


async def get_work_project_for_user(
    id: int,
    user_id: int,
    user_role: SystemUserRole,
) -> WorkProjectSchema | None:
    if not await can_access_work_project(id, user_id, user_role):
        return None
    async with get_async_session() as session:
        project = await session.get(WorkProject, id)
        if project is None:
            return None
        return await _project_schema(session, project)


async def update_work_project_metadata(
    id: int,
    request: UpdateWorkProjectMetadataRequest,
) -> WorkProjectSchema | None:
    async with get_async_session() as session:
        project = await session.get(WorkProject, id)
        if project is None:
            return None
        project.name = request.name
        project.description = request.description
        project.sandbox_container_id = request.sandbox_container_id
        project.assets_text = request.assets_text
        project.type = request.type
        project.updated_at = datetime.now()
        session.add(project)
        await _replace_project_owners(session, id, request.owner_user_ids)
        await session.commit()
        await session.refresh(project)
        schema = await _project_schema(session, project)

    logger.info("work project metadata updated: %s", id)
    return schema


async def cancel_work_project(id: int) -> tuple[WorkProjectSchema | None, bool]:
    async with get_async_session() as session:
        project = await session.get(WorkProject, id)
        if project is None:
            return None, False
        if not can_cancel_work_project(project.status):
            return await _project_schema(session, project), False

        project.status = WorkProjectStatus.CANCELED
        project.updated_at = datetime.now()
        session.add(project)
        session_ids = list((await session.exec(
            select(AgentSessionMeta.session_id).where(AgentSessionMeta.project_id == id)
        )).all())
        await session.commit()
        await session.refresh(project)
        schema = await _project_schema(session, project)

    await cancel_sessions(session_ids, "WorkProject canceled.")
    logger.info("work project canceled: %s", id)
    return schema, True


async def delete_work_project(id: int) -> bool:
    async with get_async_session() as session:
        project = await session.get(WorkProject, id)
        if project is None:
            return False
        session_ids = list((await session.exec(
            select(AgentSessionMeta.session_id).where(AgentSessionMeta.project_id == id)
        )).all())

    for session_id in session_ids:
        await delete_session(
            session_id,
            user_role=SystemUserRole.ADMIN,
            allow_project_session=True,
        )

    async with get_async_session() as session:
        project = await session.get(WorkProject, id)
        if project is None:
            return True
        await session.delete(project)
        await session.commit()

    logger.info("work project deleted: %s", id)
    return True


async def retry_work_project(id: int) -> tuple[WorkProjectSchema | None, bool]:
    async with get_async_session() as session:
        project = await session.get(WorkProject, id)
        if project is None:
            return None, False
        if not can_retry_work_project(project.status):
            return await _project_schema(session, project), False

        project.status = derive_work_project_status(project.tasks, WorkProjectStatus.WORKING)
        project.updated_at = datetime.now()
        session.add(project)
        await session.commit()
        await session.refresh(project)
        schema = await _project_schema(session, project)

    logger.debug("work project retried: %s", project.id)
    return schema, True


async def query_work_projects_for_user(
    page: int,
    size: int,
    keyword: str,
    user_id: int,
    user_role: SystemUserRole,
) -> Page[WorkProjectSchema]:
    return await _query_work_projects(
        page=page,
        size=size,
        keyword=keyword,
        owner_user_id=None if user_role == SystemUserRole.ADMIN else user_id,
    )


async def create_work_project_session(
    project_id: int,
    owner_id: int,
    user_role: SystemUserRole,
) -> WorkProjectSessionCreateResult:
    if not await can_access_work_project(project_id, owner_id, user_role):
        return WorkProjectSessionCreateResult(not_found=True)

    session_id = str(uuid4())
    async with get_async_session() as session:
        project = (await session.exec(
            select(WorkProject)
            .where(WorkProject.id == project_id)
            .with_for_update()
        )).first()
        if project is None:
            return WorkProjectSessionCreateResult(not_found=True)
        if not can_create_work_project_session(project.status):
            return WorkProjectSessionCreateResult(inactive=True)
        title = await _next_project_session_title(session, project_id)

        await ensure_sdk_session_row(session, session_id)
        session.add(AgentSessionMeta(
            session_id=session_id,
            session_type=SessionType.PROJECT,
            title=title,
            agent_code=DEFAULT_AGENT_CODE,
            owner_id=owner_id,
            project_id=project_id,
        ))
        await session.commit()

    logger.info("work project session created: project=%s session=%s", project_id, session_id)
    return WorkProjectSessionCreateResult(session_id=session_id)


async def list_work_project_sessions(
    project_id: int,
    user_id: int,
    user_role: SystemUserRole,
) -> list[AgentSessionSummarySchema] | None:
    if not await can_access_work_project(project_id, user_id, user_role):
        return None
    return await list_sessions(
        limit=100,
        user_id=user_id,
        user_role=user_role,
        project_id=project_id,
    )


async def can_run_work_project_session(
    session_id: str,
    user_id: int,
    user_role: SystemUserRole,
) -> bool:
    async with get_async_session() as session:
        meta = await session.get(AgentSessionMeta, session_id)
        if meta is None:
            return False
        if meta.project_id is None:
            return True
        if not await _can_access_work_project_in_tx(session, meta.project_id, user_id, user_role):
            return False
        project = await session.get(WorkProject, meta.project_id)
        return project is not None and can_create_work_project_session(project.status)


async def delete_work_project_session(
    project_id: int,
    session_id: str,
    user_id: int,
    user_role: SystemUserRole,
) -> bool | None:
    if not await can_access_work_project(project_id, user_id, user_role):
        return None
    async with get_async_session() as session:
        meta = await session.get(AgentSessionMeta, session_id)
        if meta is None or meta.project_id != project_id:
            return None if await session.get(WorkProject, project_id) is None else False
    return await delete_session(
        session_id,
        user_id=user_id,
        user_role=user_role,
        allow_project_session=True,
    )


async def work_project_sandbox_container_id_for_user(
    project_id: int,
    user_id: int,
    user_role: SystemUserRole,
) -> int | None:
    if not await can_access_work_project(project_id, user_id, user_role):
        return None
    async with get_async_session() as session:
        project = await session.get(WorkProject, project_id)
    return project.sandbox_container_id if project is not None else None


async def _project_schema(
    session,
    project: WorkProject,
) -> WorkProjectSchema:
    return WorkProjectSchema(**_project_schema_payload(
        project=project,
        owners=await _owners_for_project(session, project.id or 0),
        session_count=await _session_count_in_tx(session, project.id or 0),
    ))


async def _session_count_in_tx(session, project_id: int) -> int:
    if project_id <= 0:
        return 0
    return (await _session_counts(session, [project_id])).get(project_id, 0)


async def _next_project_session_title(session, project_id: int) -> str:
    rows = (await session.exec(
        select(AgentSessionMeta.title).where(AgentSessionMeta.project_id == project_id)
    )).all()
    max_number = 0
    for title in rows:
        match = _PROJECT_SESSION_TITLE_PATTERN.match(title or "")
        if match is not None:
            max_number = max(max_number, int(match.group("number")))
    return f"session {max_number + 1}"


async def _session_counts(session, project_ids: list[int]) -> dict[int, int]:
    ids = [project_id for project_id in project_ids if project_id > 0]
    if not ids:
        return {}
    rows = (await session.exec(
        select(AgentSessionMeta.project_id, func.count())
        .where(AgentSessionMeta.project_id.in_(ids))
        .group_by(AgentSessionMeta.project_id)
    )).all()
    return {int(project_id): int(count) for project_id, count in rows if project_id is not None}


async def _query_work_projects(
    page: int,
    size: int,
    keyword: str,
    owner_user_id: int | None = None,
) -> Page[WorkProjectSchema]:
    statement = select(WorkProject).order_by(WorkProject.id)

    keyword = keyword.strip()
    if keyword:
        pattern = f"%{keyword}%"
        statement = statement.where(
            or_(
                WorkProject.name.ilike(pattern),
                WorkProject.description.ilike(pattern),
                cast(WorkProject.status, String).ilike(pattern),
                cast(WorkProject.type, String).ilike(pattern),
            )
        )
    if owner_user_id is not None:
        statement = statement.join(
            WorkProjectOwner,
            WorkProjectOwner.project_id == WorkProject.id,
        ).where(WorkProjectOwner.user_id == owner_user_id)
    page_result = await paginate_statement(statement, page=page, size=size)
    projects = page_result.items

    async with get_async_session() as session:
        counts = await _session_counts(session, [project.id or 0 for project in projects])
        owners = await _owners_by_project(session, [project.id or 0 for project in projects])

        items = [
            WorkProjectSchema(**_project_schema_payload(
                project=project,
                owners=owners.get(project.id or 0, []),
                session_count=counts.get(project.id or 0, 0),
            ))
            for project in projects
        ]
    return Page(page=page_result.page, size=page_result.size, total=page_result.total, items=items)


def _project_schema_payload(
    project: WorkProject,
    owners: list[WorkProjectOwnerSchema],
    session_count: int,
) -> dict:
    return {
        "id": project.id or 0,
        "name": project.name,
        "description": project.description,
        "owner_user_ids": [owner.id for owner in owners],
        "owners": owners,
        "sandbox_container_id": project.sandbox_container_id,
        "assets_text": project.assets_text,
        "tasks": [WorkProjectTaskSchema.model_validate(item) for item in project.tasks],
        "agent_summaries": [
            WorkProjectAgentSummarySchema.model_validate(summary)
            for summary in (project.agent_summaries or {}).values()
            if isinstance(summary, dict)
        ],
        "progress": project.progress,
        "session_count": session_count,
        "status": project.status,
        "can_create_session": can_create_work_project_session(project.status),
        "can_cancel": can_cancel_work_project(project.status),
        "can_retry": can_retry_work_project(project.status),
        "type": project.type,
        "created_at": project.created_at,
        "updated_at": project.updated_at,
    }


async def _owners_by_project(session, project_ids: list[int]) -> dict[int, list[WorkProjectOwnerSchema]]:
    ids = [project_id for project_id in project_ids if project_id > 0]
    if not ids:
        return {}
    rows = (await session.exec(
        select(WorkProjectOwner, SystemUser)
        .join(SystemUser, SystemUser.id == WorkProjectOwner.user_id)
        .where(WorkProjectOwner.project_id.in_(ids))
        .order_by(WorkProjectOwner.project_id, WorkProjectOwner.position, WorkProjectOwner.user_id)
    )).all()
    result: dict[int, list[WorkProjectOwnerSchema]] = {project_id: [] for project_id in ids}
    for owner, user in rows:
        if user.id is None:
            continue
        result.setdefault(owner.project_id, []).append(_owner_schema(user))
    return result


async def _owners_for_project(session, project_id: int) -> list[WorkProjectOwnerSchema]:
    return (await _owners_by_project(session, [project_id])).get(project_id, [])


def _owner_schema(user: SystemUser) -> WorkProjectOwnerSchema:
    return WorkProjectOwnerSchema(
        id=user.id or 0,
        role=user.role,
        username=user.username,
    )


def _set_project_owner_rows(session, project_id: int, owner_user_ids: list[int]) -> None:
    for position, user_id in enumerate(owner_user_ids):
        session.add(WorkProjectOwner(project_id=project_id, user_id=user_id, position=position))


async def _replace_project_owners(session, project_id: int, owner_user_ids: list[int]) -> None:
    for owner in (await session.exec(
        select(WorkProjectOwner).where(WorkProjectOwner.project_id == project_id)
    )).all():
        await session.delete(owner)
    _set_project_owner_rows(session, project_id, owner_user_ids)


async def can_access_work_project(
    project_id: int,
    user_id: int,
    user_role: SystemUserRole,
) -> bool:
    async with get_async_session() as session:
        return await _can_access_work_project_in_tx(session, project_id, user_id, user_role)


async def _can_access_work_project_in_tx(
    session,
    project_id: int,
    user_id: int,
    user_role: SystemUserRole,
) -> bool:
    if await session.get(WorkProject, project_id) is None:
        return False
    if user_role == SystemUserRole.ADMIN:
        return True
    return await session.get(WorkProjectOwner, (project_id, user_id)) is not None
