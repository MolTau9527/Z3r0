from http import HTTPStatus

from middleware.auth import AuthUser
from schema.common.responses import CommonResponse
from schema.work_project.findings import QueryWorkProjectFindingsResponse
from service.common.pagination import paginated_payload
from service.work_project.findings import query_work_project_findings
from service.work_project.projects import can_access_work_project


async def query_work_project_findings_handler(
    project_id: int,
    page: int,
    size: int,
    keyword: str,
    user: AuthUser,
) -> CommonResponse:
    if not await can_access_work_project(project_id, user.id, user.role):
        return CommonResponse(code=HTTPStatus.NOT_FOUND.value, message="work project not found")
    findings = await query_work_project_findings(project_id, page=page, size=size, keyword=keyword)
    return CommonResponse(data=QueryWorkProjectFindingsResponse(**paginated_payload(findings, findings.items)))
