import type { components, paths } from "./generated/schema";

type JsonRequestBody<Operation> = Operation extends {
  requestBody: { content: { "application/json": infer Body } };
}
  ? Body
  : never;

type MultipartRequestBody<Operation> = Operation extends {
  requestBody: { content: { "multipart/form-data": infer Body } };
}
  ? Body
  : never;

type JsonResponse<Operation> = Operation extends {
  responses: { 200: { content: { "application/json": infer Response } } };
}
  ? Response
  : never;

type QueryParameters<Operation> = Operation extends { parameters: { query?: infer Query } } ? Query : never;
type PathParameters<Operation> = Operation extends { parameters: { path?: infer Params } } ? Params : never;

export type CommonResponsePayload = components["schemas"]["CommonResponse"];

export type LoginRequest = JsonRequestBody<paths["/api/system-users/login"]["post"]>;
export type LoginResponse = JsonResponse<paths["/api/system-users/login"]["post"]>;

export type QuerySystemUsersParams = QueryParameters<paths["/api/system-users"]["get"]>;
export type QuerySystemUsersResponse = JsonResponse<paths["/api/system-users"]["get"]>;
export type QuerySystemUsersData = NonNullable<QuerySystemUsersResponse["data"]>;
export type SystemUser = QuerySystemUsersData["items"][number];
export type SystemUserRole = components["schemas"]["SystemUserRole"];

export type CreateSystemUserRequest = JsonRequestBody<paths["/api/system-users"]["post"]>;
export type CreateSystemUserResponse = JsonResponse<paths["/api/system-users"]["post"]>;

export type SystemUserPathParams = PathParameters<paths["/api/system-users/{id}"]["patch"]>;
export type UpdateSystemUserRequest = JsonRequestBody<paths["/api/system-users/{id}"]["patch"]>;
export type UpdateSystemUserResponse = JsonResponse<paths["/api/system-users/{id}"]["patch"]>;
export type DeleteSystemUserResponse = JsonResponse<paths["/api/system-users/{id}"]["delete"]>;

export type QuerySandboxImagesParams = QueryParameters<paths["/api/sandbox-images"]["get"]>;
export type QuerySandboxImagesResponse = JsonResponse<paths["/api/sandbox-images"]["get"]>;
export type QuerySandboxImagesData = NonNullable<QuerySandboxImagesResponse["data"]>;
export type SandboxImage = QuerySandboxImagesData["items"][number];
export type SandboxImageStatus = components["schemas"]["SandboxImageStatus"];

export type CreateSandboxImageRequest = JsonRequestBody<paths["/api/sandbox-images"]["post"]>;
export type CreateSandboxImageResponse = JsonResponse<paths["/api/sandbox-images"]["post"]>;

export type SandboxImagePathParams = PathParameters<paths["/api/sandbox-images/{id}"]["delete"]>;
export type DeleteSandboxImageResponse = JsonResponse<paths["/api/sandbox-images/{id}"]["delete"]>;
export type CancelSandboxImagePathParams = PathParameters<paths["/api/sandbox-images/{id}/cancel"]["post"]>;
export type CancelSandboxImageResponse = JsonResponse<paths["/api/sandbox-images/{id}/cancel"]["post"]>;
export type RetrySandboxImagePathParams = PathParameters<paths["/api/sandbox-images/{id}/retry"]["post"]>;
export type RetrySandboxImageResponse = JsonResponse<paths["/api/sandbox-images/{id}/retry"]["post"]>;

export type QuerySandboxContainersParams = QueryParameters<paths["/api/sandbox-containers"]["get"]>;
export type QuerySandboxContainersResponse = JsonResponse<paths["/api/sandbox-containers"]["get"]>;
export type QuerySandboxContainersData = NonNullable<QuerySandboxContainersResponse["data"]>;
export type SandboxContainer = QuerySandboxContainersData["items"][number];
export type QueryAvailableSandboxContainersParams = QueryParameters<paths["/api/sandbox-containers/available"]["get"]>;
export type QueryAvailableSandboxContainersResponse = JsonResponse<paths["/api/sandbox-containers/available"]["get"]>;
export type GenerateDefaultSandboxContainerPortMappingsParams = QueryParameters<paths["/api/sandbox-containers/default-port-mappings"]["get"]>;
export type GenerateDefaultSandboxContainerPortMappingsResponse = JsonResponse<paths["/api/sandbox-containers/default-port-mappings"]["get"]>;
export type SandboxContainerStatus = components["schemas"]["SandboxContainerStatus"];
export type SandboxContainerPortMapping = components["schemas"]["SandboxContainerPortMapping"];

export type CreateSandboxContainerRequest = JsonRequestBody<paths["/api/sandbox-containers"]["post"]>;
export type CreateSandboxContainerResponse = JsonResponse<paths["/api/sandbox-containers"]["post"]>;

export type SandboxContainerPathParams = PathParameters<paths["/api/sandbox-containers/{id}"]["delete"]>;
export type DeleteSandboxContainerResponse = JsonResponse<paths["/api/sandbox-containers/{id}"]["delete"]>;
export type StartSandboxContainerPathParams = PathParameters<paths["/api/sandbox-containers/{id}/start"]["post"]>;
export type StartSandboxContainerResponse = JsonResponse<paths["/api/sandbox-containers/{id}/start"]["post"]>;
export type StopSandboxContainerPathParams = PathParameters<paths["/api/sandbox-containers/{id}/stop"]["post"]>;
export type StopSandboxContainerResponse = JsonResponse<paths["/api/sandbox-containers/{id}/stop"]["post"]>;

export type ListContainerFilesParams = QueryParameters<paths["/api/sandbox-containers/{id}/files"]["get"]>;
export type ListContainerFilesResponse = JsonResponse<paths["/api/sandbox-containers/{id}/files"]["get"]>;
export type ContainerFileInfo = components["schemas"]["ContainerFileInfo"];
export type ContainerFileType = components["schemas"]["ContainerFileType"];

export type ReadContainerFileParams = QueryParameters<paths["/api/sandbox-containers/{id}/files/read"]["get"]>;
export type ReadContainerFileResponse = JsonResponse<paths["/api/sandbox-containers/{id}/files/read"]["get"]>;

export type ContainerFileWriteRequest = JsonRequestBody<paths["/api/sandbox-containers/{id}/files/write"]["post"]>;
export type ContainerFileWriteResponse = JsonResponse<paths["/api/sandbox-containers/{id}/files/write"]["post"]>;
export type ContainerFileUploadRequest = MultipartRequestBody<paths["/api/sandbox-containers/{id}/files/upload"]["post"]>;
export type ContainerFileUploadResponse = JsonResponse<paths["/api/sandbox-containers/{id}/files/upload"]["post"]>;
export type DownloadContainerFilesParams = QueryParameters<paths["/api/sandbox-containers/{id}/files/download"]["get"]>;
export type ContainerFileCopyRequest = JsonRequestBody<paths["/api/sandbox-containers/{id}/files/copy"]["post"]>;
export type ContainerFileCopyResponse = JsonResponse<paths["/api/sandbox-containers/{id}/files/copy"]["post"]>;
export type ContainerFileMoveRequest = JsonRequestBody<paths["/api/sandbox-containers/{id}/files/move"]["post"]>;
export type ContainerFileMoveResponse = JsonResponse<paths["/api/sandbox-containers/{id}/files/move"]["post"]>;
export type ContainerFileDeleteRequest = JsonRequestBody<paths["/api/sandbox-containers/{id}/files/delete"]["post"]>;
export type ContainerFileDeleteResponse = JsonResponse<paths["/api/sandbox-containers/{id}/files/delete"]["post"]>;
export type ContainerFileMkdirRequest = JsonRequestBody<paths["/api/sandbox-containers/{id}/files/mkdir"]["post"]>;
export type ContainerFileMkdirResponse = JsonResponse<paths["/api/sandbox-containers/{id}/files/mkdir"]["post"]>;

export type QueryWorkProjectsParams = QueryParameters<paths["/api/work-projects"]["get"]>;
export type QueryWorkProjectsResponse = JsonResponse<paths["/api/work-projects"]["get"]>;
export type QueryWorkProjectsData = NonNullable<QueryWorkProjectsResponse["data"]>;
export type WorkProject = QueryWorkProjectsData["items"][number];
export type WorkProjectStatus = components["schemas"]["WorkProjectStatus"];
export type WorkProjectType = components["schemas"]["WorkProjectType"];

export type CreateWorkProjectRequest = JsonRequestBody<paths["/api/work-projects"]["post"]>;
export type CreateWorkProjectResponse = JsonResponse<paths["/api/work-projects"]["post"]>;

export type WorkProjectPathParams = PathParameters<paths["/api/work-projects/{id}"]["delete"]>;
export type DeleteWorkProjectResponse = JsonResponse<paths["/api/work-projects/{id}"]["delete"]>;
export type CancelWorkProjectPathParams = PathParameters<paths["/api/work-projects/{id}/cancel"]["post"]>;
export type CancelWorkProjectResponse = JsonResponse<paths["/api/work-projects/{id}/cancel"]["post"]>;
export type RetryWorkProjectPathParams = PathParameters<paths["/api/work-projects/{id}/retry"]["post"]>;
export type RetryWorkProjectResponse = JsonResponse<paths["/api/work-projects/{id}/retry"]["post"]>;

export type AgentSessionSummary = components["schemas"]["AgentSessionSummarySchema"];
export type SessionType = components["schemas"]["SessionType"];

export type AgentInfo = components["schemas"]["AgentInfoSchema"];
export type ListAgentsResponse = JsonResponse<paths["/api/agents"]["get"]>;
export type ListAgentsData = NonNullable<ListAgentsResponse["data"]>;

export type ListAgentSessionsResponse = JsonResponse<paths["/api/agent-sessions"]["get"]>;
export type ListAgentSessionsData = NonNullable<ListAgentSessionsResponse["data"]>;

export type CreateAgentSessionResponse = JsonResponse<paths["/api/agent-sessions"]["post"]>;
export type CreateAgentSessionData = NonNullable<CreateAgentSessionResponse["data"]>;

export type ListAgentEventsResponse = JsonResponse<paths["/api/agent-sessions/{session_id}/events"]["get"]>;
export type ListAgentEventsData = NonNullable<ListAgentEventsResponse["data"]>;
export type DeleteAgentSessionResponse = JsonResponse<paths["/api/agent-sessions/{session_id}"]["delete"]>;

export type UserMessageEvent = components["schemas"]["UserMessageEvent"];
export type TextDeltaEvent = components["schemas"]["TextDeltaEvent"];
export type TextCompleteEvent = components["schemas"]["TextCompleteEvent"];
export type ThinkingDeltaEvent = components["schemas"]["ThinkingDeltaEvent"];
export type ThinkingCompleteEvent = components["schemas"]["ThinkingCompleteEvent"];
export type ToolCallEvent = components["schemas"]["ToolCallEvent"];
export type ToolResultEvent = components["schemas"]["ToolResultEvent"];
export type SubagentTaskEvent = components["schemas"]["SubagentTaskEvent"];
export type AgentSubordinateStatus = components["schemas"]["AgentSubordinateStatus"];
export type ErrorEvent = components["schemas"]["ErrorEvent"];
export type DoneEvent = components["schemas"]["DoneEvent"];

export type AgentContentEvent = ListAgentEventsData["items"][number];
export type AgentStreamEvent = AgentContentEvent | DoneEvent;

export type AgentStreamCommand = components["schemas"]["AgentStreamCommandSchema"];
