import { apiDelete, apiGet, apiPatch, apiPost } from "./client";
import { buildQuery } from "./query";
import type {
  CancelWorkProjectPathParams,
  CancelWorkProjectResponse,
  CreateWorkProjectRequest,
  CreateWorkProjectResponse,
  CreateWorkProjectSessionResponse,
  DeleteWorkProjectSessionResponse,
  DeleteWorkProjectResponse,
  GetWorkProjectResponse,
  GetWorkProjectGraphResponse,
  ListWorkProjectSessionsResponse,
  QueryWorkProjectAssetsParams,
  QueryWorkProjectAssetsResponse,
  QueryWorkProjectFindingsParams,
  QueryWorkProjectFindingsResponse,
  QueryWorkProjectsParams,
  QueryWorkProjectsResponse,
  RetryWorkProjectPathParams,
  RetryWorkProjectResponse,
  UpdateWorkProjectMetadataRequest,
  UpdateWorkProjectMetadataResponse,
  WorkProjectAssetsPathParams,
  WorkProjectFindingsPathParams,
  WorkProjectGraphPathParams,
  WorkProjectPathParams,
} from "./types";

const WORK_PROJECTS_PATH = "/api/work-projects";

export function queryWorkProjects(params: QueryWorkProjectsParams) {
  return apiGet<QueryWorkProjectsResponse>(`${WORK_PROJECTS_PATH}${buildQuery(params)}`);
}

export function createWorkProject(payload: CreateWorkProjectRequest) {
  return apiPost<CreateWorkProjectResponse>(WORK_PROJECTS_PATH, payload);
}

export function getWorkProject(id: WorkProjectPathParams["id"]) {
  return apiGet<GetWorkProjectResponse>(`${WORK_PROJECTS_PATH}/${id}`);
}

export function updateWorkProjectMetadata(id: WorkProjectPathParams["id"], payload: UpdateWorkProjectMetadataRequest) {
  return apiPatch<UpdateWorkProjectMetadataResponse>(`${WORK_PROJECTS_PATH}/${id}/metadata`, payload);
}

export function listWorkProjectSessions(id: WorkProjectPathParams["id"]) {
  return apiGet<ListWorkProjectSessionsResponse>(`${WORK_PROJECTS_PATH}/${id}/sessions`);
}

export function createWorkProjectSession(id: WorkProjectPathParams["id"]) {
  return apiPost<CreateWorkProjectSessionResponse>(`${WORK_PROJECTS_PATH}/${id}/sessions`);
}

export function deleteWorkProjectSession(id: WorkProjectPathParams["id"], sessionId: string) {
  return apiDelete<DeleteWorkProjectSessionResponse>(
    `${WORK_PROJECTS_PATH}/${id}/sessions/${encodeURIComponent(sessionId)}`,
  );
}

export function cancelWorkProject(id: CancelWorkProjectPathParams["id"]) {
  return apiPost<CancelWorkProjectResponse>(`${WORK_PROJECTS_PATH}/${id}/cancel`);
}

export function retryWorkProject(id: RetryWorkProjectPathParams["id"]) {
  return apiPost<RetryWorkProjectResponse>(`${WORK_PROJECTS_PATH}/${id}/retry`);
}

export function deleteWorkProject(id: WorkProjectPathParams["id"]) {
  return apiDelete<DeleteWorkProjectResponse>(`${WORK_PROJECTS_PATH}/${id}`);
}

export function queryWorkProjectAssets(
  projectId: WorkProjectAssetsPathParams["project_id"],
  params: QueryWorkProjectAssetsParams,
) {
  return apiGet<QueryWorkProjectAssetsResponse>(
    `${WORK_PROJECTS_PATH}/${projectId}/assets${buildQuery(params)}`,
  );
}

export function queryWorkProjectFindings(
  projectId: WorkProjectFindingsPathParams["project_id"],
  params: QueryWorkProjectFindingsParams,
) {
  return apiGet<QueryWorkProjectFindingsResponse>(
    `${WORK_PROJECTS_PATH}/${projectId}/findings${buildQuery(params)}`,
  );
}

export function getWorkProjectGraph(projectId: WorkProjectGraphPathParams["project_id"]) {
  return apiGet<GetWorkProjectGraphResponse>(`${WORK_PROJECTS_PATH}/${projectId}/graph`);
}
