import json
from datetime import datetime

from agents import RunContextWrapper, function_tool
from sqlalchemy import Text, cast, func, literal, update
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, array
from sqlmodel import select

from core.runtime.context import AgentRuntimeContext
from database import get_async_session
from model.work_project.projects import WorkProject, WorkProjectOwner
from schema.common.tool_results import ToolResultSchema, ToolResultStatusSchema, ToolResultTypeSchema
from schema.work_project.projects import (
    WorkProjectAgentSummaryContentSchema,
    WorkProjectAgentSummarySchema,
    WorkProjectTaskSchema,
)
from service.work_project.progress import calculate_work_project_progress, derive_work_project_status


_CSO_AGENT_CODE = "cso"


@function_tool
async def load_work_project_metadata(ctx: RunContextWrapper[AgentRuntimeContext]) -> str:
    """Load basic metadata for the current WorkProject."""
    project = await _current_project(ctx.context)
    if project is None:
        return _error("No WorkProject is bound to this session.")
    return _success(await _metadata_payload(project))


@function_tool
async def load_work_project_target_assets(ctx: RunContextWrapper[AgentRuntimeContext]) -> str:
    """Load the raw target asset list for the current WorkProject."""
    project = await _current_project(ctx.context)
    if project is None:
        return _error("No WorkProject is bound to this session.")
    return _success({
        "project_id": project.id,
        "assets_text": project.assets_text,
    })


@function_tool
async def load_work_project_tasks(ctx: RunContextWrapper[AgentRuntimeContext]) -> str:
    """Load the current WorkProject task progress list."""
    project = await _current_project(ctx.context)
    if project is None:
        return _error("No WorkProject is bound to this session.")
    return _success(_tasks_payload(project))


@function_tool
async def load_work_project_agent_summaries(ctx: RunContextWrapper[AgentRuntimeContext]) -> str:
    """Load all structured agent summaries for the current WorkProject."""
    project = await _current_project(ctx.context)
    if project is None:
        return _error("No WorkProject is bound to this session.")
    return _success(_agent_summaries_payload(project))


@function_tool
async def update_work_project_agent_summary(
    ctx: RunContextWrapper[AgentRuntimeContext],
    summary: WorkProjectAgentSummaryContentSchema,
) -> str:
    """Replace this agent's live structured task summary for the current WorkProject.

    Each agent can only write its own summary slot, keyed by agent_code.
    Call after meaningful discoveries, useful negative results, evidence, blockers,
    decisions, handoffs, or progress changes, before the next command/subagent/wait/user reply when practical.
    Do not wait until the task is done.
    Use task_id/task_title and progress (0-100, at most two decimals) to report this agent's current subtask progress.

    Args:
        summary: Full replacement of this agent's current summary. Include task_id/task_title,
            progress, status, findings, decisions, blockers, next_steps, evidence, and notes as applicable.
            When task_id or exact task_title matches a shared task, that task's progress is synchronized
            and project progress is recalculated.
    """
    agent_code = ctx.context.agent_code.strip()
    if not agent_code:
        return _error("Agent code is required.")
    project_id = ctx.context.work_project_id
    if project_id is None:
        return _error("No WorkProject is bound to this session.")

    now = datetime.now()
    payload = {
        "agent_code": agent_code,
        "summary": summary.model_dump(mode="json"),
        "updated_at": now.isoformat(),
    }
    async with get_async_session() as session:
        result = await session.execute(
            update(WorkProject)
            .where(WorkProject.id == project_id)
            .values(
                agent_summaries=func.jsonb_set(
                    func.coalesce(WorkProject.agent_summaries, literal({}, type_=JSONB)),
                    cast(array([agent_code]), ARRAY(Text)),
                    literal(payload, type_=JSONB),
                    True,
                ),
                updated_at=now,
            )
            .returning(WorkProject.id)
        )
        updated_project_id = result.scalar_one_or_none()
        if updated_project_id is None:
            await session.rollback()
            return _error("WorkProject not found.")
        project = (await session.exec(
            select(WorkProject)
            .where(WorkProject.id == project_id)
            .with_for_update()
        )).first()
        if project is None:
            await session.rollback()
            return _error("WorkProject not found.")
        _sync_summary_progress_to_task(project, summary)
        session.add(project)
        await session.commit()
        await session.refresh(project)

    return _success(_agent_summaries_payload(project))


@function_tool
async def update_work_project_tasks(
    ctx: RunContextWrapper[AgentRuntimeContext],
    tasks: list[WorkProjectTaskSchema],
) -> str:
    """Replace the current WorkProject task progress list.

    Only the chief security officer agent (`cso`) can update the shared project task list.
    Call when global task state changes, including after your own progress or subagent results,
    before reporting or delegating more work when practical.
    Each task status must be one of: todo, in_progress, blocked, done.
    Each task progress value must be 0-100 with at most two decimal places.

    Args:
        tasks: Full replacement of the shared task list. Preserve existing tasks that still matter,
            update status/progress/summary, and add or remove tasks only when the project plan changes.
    """
    if ctx.context.agent_code != _CSO_AGENT_CODE:
        return _error("Only the cso agent can update WorkProject task progress.")
    project_id = ctx.context.work_project_id
    if project_id is None:
        return _error("No WorkProject is bound to this session.")

    async with get_async_session() as session:
        project = await session.get(WorkProject, project_id)
        if project is None:
            return _error("WorkProject not found.")
        project.tasks = [task.model_dump(mode="json") for task in tasks]
        project.progress = calculate_work_project_progress(project.tasks)
        project.status = derive_work_project_status(project.tasks, project.status)
        project.updated_at = datetime.now()
        session.add(project)
        await session.commit()
        await session.refresh(project)

    return _success(_tasks_payload(project))


async def _current_project(context: AgentRuntimeContext) -> WorkProject | None:
    if context.work_project_id is None:
        return None
    async with get_async_session() as session:
        return await session.get(WorkProject, context.work_project_id)


async def _current_project_owner_ids(project_id: int | None) -> list[int]:
    if project_id is None:
        return []
    async with get_async_session() as session:
        rows = (await session.exec(
            select(WorkProjectOwner.user_id)
            .where(WorkProjectOwner.project_id == project_id)
            .order_by(WorkProjectOwner.position, WorkProjectOwner.user_id)
        )).all()
    return list(rows)


async def _metadata_payload(project: WorkProject) -> dict:
    return {
        "project_id": project.id,
        "name": project.name,
        "description": project.description,
        "owner_user_ids": await _current_project_owner_ids(project.id),
        "sandbox_container_id": project.sandbox_container_id,
        "status": project.status,
        "type": project.type,
        "created_at": project.created_at.isoformat(),
        "updated_at": project.updated_at.isoformat(),
    }


def _tasks_payload(project: WorkProject) -> dict:
    return {
        "project_id": project.id,
        "progress": project.progress,
        "tasks": [
            WorkProjectTaskSchema.model_validate(task).model_dump(mode="json")
            for task in project.tasks
        ],
    }


def _agent_summaries_payload(project: WorkProject) -> dict:
    summaries = [
        WorkProjectAgentSummarySchema.model_validate(summary).model_dump(mode="json")
        for summary in (project.agent_summaries or {}).values()
        if isinstance(summary, dict)
    ]
    return {
        "project_id": project.id,
        "agent_summaries": summaries,
    }


def _sync_summary_progress_to_task(
    project: WorkProject,
    summary: WorkProjectAgentSummaryContentSchema,
) -> None:
    task_id = summary.task_id.strip()
    task_title = summary.task_title.strip()
    if not task_id and not task_title:
        return

    tasks: list[dict] = []
    changed = False
    for raw_task in project.tasks:
        task = WorkProjectTaskSchema.model_validate(raw_task)
        if task.id == task_id or (not task_id and task.title == task_title):
            task.progress = summary.progress
            changed = True
        tasks.append(task.model_dump(mode="json"))

    if not changed:
        return
    project.tasks = tasks
    project.progress = calculate_work_project_progress(tasks)
    project.status = derive_work_project_status(tasks, project.status)


def _success(payload: object) -> str:
    return ToolResultSchema(
        status=ToolResultStatusSchema.SUCCESS,
        type=ToolResultTypeSchema.KNOWLEDGE_DETAIL,
        output=json.dumps(payload, ensure_ascii=False),
    ).model_dump_json()


def _error(message: str) -> str:
    return ToolResultSchema(
        status=ToolResultStatusSchema.ERROR,
        type=ToolResultTypeSchema.KNOWLEDGE_DETAIL,
        output=message,
    ).model_dump_json()
