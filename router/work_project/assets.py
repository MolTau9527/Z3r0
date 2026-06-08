from fastapi import APIRouter, Depends, Query

from handler.work_project.assets import query_work_project_assets_handler
from middleware.auth import AuthUser, require_user
from router.common.responses import COMMON_ERROR_RESPONSES
from schema.common.responses import CommonResponse
from schema.work_project.assets import QueryWorkProjectAssetsResponse


router = APIRouter(
    prefix="/work-projects/{project_id}/assets",
    tags=["work-project-assets"],
    dependencies=[Depends(require_user)],
)


async def query_work_project_assets_route(
    project_id: int,
    page: int = Query(default=1, ge=1),
    size: int = Query(default=100, ge=1, le=100),
    keyword: str = Query(default=""),
    user: AuthUser = Depends(require_user),
) -> CommonResponse[QueryWorkProjectAssetsResponse]:
    return await query_work_project_assets_handler(project_id, page, size, keyword, user)


router.add_api_route(
    "",
    query_work_project_assets_route,
    methods=["GET"],
    response_model=CommonResponse[QueryWorkProjectAssetsResponse],
    responses=COMMON_ERROR_RESPONSES,
)
