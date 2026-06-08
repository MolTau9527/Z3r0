from fastapi import APIRouter, Depends

from handler.work_project.graph import get_work_project_graph_snapshot_handler
from middleware.auth import AuthUser, require_user
from router.common.responses import COMMON_ERROR_RESPONSES
from schema.common.responses import CommonResponse
from schema.work_project.graph import WorkProjectGraphSnapshotSchema


router = APIRouter(
    prefix="/work-projects/{project_id}",
    tags=["work-project-graph"],
    dependencies=[Depends(require_user)],
)


async def get_work_project_graph_snapshot_route(
    project_id: int,
    user: AuthUser = Depends(require_user),
) -> CommonResponse[WorkProjectGraphSnapshotSchema]:
    return await get_work_project_graph_snapshot_handler(project_id, user)


router.add_api_route(
    "/graph",
    get_work_project_graph_snapshot_route,
    methods=["GET"],
    response_model=CommonResponse[WorkProjectGraphSnapshotSchema],
    responses=COMMON_ERROR_RESPONSES,
)
