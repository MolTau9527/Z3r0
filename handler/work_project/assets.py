from http import HTTPStatus

from middleware.auth import AuthUser
from schema.common.responses import CommonResponse
from schema.work_project.assets import QueryWorkProjectAssetsResponse
from service.common.pagination import paginated_payload
from service.work_project.assets import query_work_project_assets
from service.work_project.projects import can_access_work_project


async def query_work_project_assets_handler(
    project_id: int,
    page: int,
    size: int,
    keyword: str,
    user: AuthUser,
) -> CommonResponse:
    if not await can_access_work_project(project_id, user.id, user.role):
        return CommonResponse(code=HTTPStatus.NOT_FOUND.value, message="work project not found")
    assets = await query_work_project_assets(project_id, page=page, size=size, keyword=keyword)
    return CommonResponse(data=QueryWorkProjectAssetsResponse(**paginated_payload(assets, assets.items)))
