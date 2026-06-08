from fastapi import APIRouter, Depends, Query

from handler.work_project.findings import query_work_project_findings_handler
from middleware.auth import AuthUser, require_user
from router.common.responses import COMMON_ERROR_RESPONSES
from schema.common.responses import CommonResponse
from schema.work_project.findings import QueryWorkProjectFindingsResponse


router = APIRouter(
    prefix="/work-projects/{project_id}/findings",
    tags=["work-project-findings"],
    dependencies=[Depends(require_user)],
)


async def query_work_project_findings_route(
    project_id: int,
    page: int = Query(default=1, ge=1),
    size: int = Query(default=100, ge=1, le=100),
    keyword: str = Query(default=""),
    user: AuthUser = Depends(require_user),
) -> CommonResponse[QueryWorkProjectFindingsResponse]:
    return await query_work_project_findings_handler(project_id, page, size, keyword, user)


router.add_api_route(
    "",
    query_work_project_findings_route,
    methods=["GET"],
    response_model=CommonResponse[QueryWorkProjectFindingsResponse],
    responses=COMMON_ERROR_RESPONSES,
)
