from http import HTTPStatus

from middleware.auth import AuthUser
from schema.common.responses import CommonResponse
from service.work_project.graph import get_work_project_graph_snapshot
from service.work_project.projects import can_access_work_project


async def get_work_project_graph_snapshot_handler(project_id: int, user: AuthUser) -> CommonResponse:
    if not await can_access_work_project(project_id, user.id, user.role):
        return CommonResponse(code=HTTPStatus.NOT_FOUND.value, message="work project not found")
    return CommonResponse(data=await get_work_project_graph_snapshot(project_id))
