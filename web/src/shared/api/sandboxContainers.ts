import { clearStoredAccessToken, getStoredAccessToken } from "../auth/session";
import { ApiError, apiRequest, buildAuthenticatedWebSocketUrl } from "./client";
import { buildQuery } from "./query";
import type {
  ContainerFileCopyRequest,
  ContainerFileCopyResponse,
  ContainerFileDeleteRequest,
  ContainerFileDeleteResponse,
  ContainerFileMkdirRequest,
  ContainerFileMkdirResponse,
  ContainerFileMoveRequest,
  ContainerFileMoveResponse,
  ContainerFileUploadRequest,
  ContainerFileUploadResponse,
  ContainerFileWriteRequest,
  ContainerFileWriteResponse,
  CreateSandboxContainerRequest,
  CreateSandboxContainerResponse,
  DeleteSandboxContainerResponse,
  GenerateDefaultSandboxContainerPortMappingsParams,
  GenerateDefaultSandboxContainerPortMappingsResponse,
  ListContainerFilesParams,
  ListContainerFilesResponse,
  DownloadContainerFilesParams,
  QueryAvailableSandboxContainersParams,
  QueryAvailableSandboxContainersResponse,
  QuerySandboxContainersParams,
  QuerySandboxContainersResponse,
  ReadContainerFileParams,
  ReadContainerFileResponse,
  SandboxContainer,
  SandboxContainerPathParams,
  StartSandboxContainerPathParams,
  StartSandboxContainerResponse,
  StopSandboxContainerPathParams,
  StopSandboxContainerResponse,
} from "./types";

const SANDBOX_CONTAINERS_PATH = "/api/sandbox-containers";

export function querySandboxContainers(params: QuerySandboxContainersParams) {
  return apiRequest<QuerySandboxContainersResponse>(`${SANDBOX_CONTAINERS_PATH}${buildQuery(params)}`);
}

export function queryAvailableSandboxContainers(params: QueryAvailableSandboxContainersParams) {
  return apiRequest<QueryAvailableSandboxContainersResponse>(`${SANDBOX_CONTAINERS_PATH}/available${buildQuery(params)}`);
}

export function generateDefaultSandboxContainerPortMappings(params: GenerateDefaultSandboxContainerPortMappingsParams) {
  return apiRequest<GenerateDefaultSandboxContainerPortMappingsResponse>(`${SANDBOX_CONTAINERS_PATH}/default-port-mappings${buildQuery(params)}`);
}

export function createSandboxContainer(payload: CreateSandboxContainerRequest) {
  return apiRequest<CreateSandboxContainerResponse>(SANDBOX_CONTAINERS_PATH, {
    method: "POST",
    body: payload,
  });
}

export function startSandboxContainer(id: StartSandboxContainerPathParams["id"]) {
  return apiRequest<StartSandboxContainerResponse>(`${SANDBOX_CONTAINERS_PATH}/${id}/start`, {
    method: "POST",
  });
}

export function stopSandboxContainer(id: StopSandboxContainerPathParams["id"]) {
  return apiRequest<StopSandboxContainerResponse>(`${SANDBOX_CONTAINERS_PATH}/${id}/stop`, {
    method: "POST",
  });
}

export function deleteSandboxContainer(id: SandboxContainerPathParams["id"]) {
  return apiRequest<DeleteSandboxContainerResponse>(`${SANDBOX_CONTAINERS_PATH}/${id}`, {
    method: "DELETE",
  });
}

export function buildContainerShellUrl(containerHash: string) {
  return buildAuthenticatedWebSocketUrl(`${SANDBOX_CONTAINERS_PATH}/${encodeURIComponent(containerHash)}/shell`);
}

export function getContainerNoVNCPortMapping(container: SandboxContainer) {
  if (!container.novnc_support || !container.novnc_port) return undefined;
  return container.port_mappings.find((item) => (
    item.protocol === "tcp" && item.container_port === container.novnc_port
  ));
}

export function canOpenContainerNoVNC(container: SandboxContainer) {
  return Boolean(getContainerNoVNCPortMapping(container));
}

export function buildContainerNoVNCUrl(container: SandboxContainer) {
  const mapping = getContainerNoVNCPortMapping(container);
  if (!mapping) {
    throw new Error("missing noVNC port mapping");
  }

  const url = new URL(window.location.href);
  url.port = String(mapping.host_port);
  url.pathname = "/novnc/vnc.html";
  url.search = "";
  url.hash = "";
  url.searchParams.set("autoconnect", "true");
  url.searchParams.set("resize", "remote");
  url.searchParams.set("path", "websockify");
  return url.toString();
}


// ── container file operations ──────────────────────────────────────────────

export function listContainerFiles(id: number, params: ListContainerFilesParams) {
  return apiRequest<ListContainerFilesResponse>(`${SANDBOX_CONTAINERS_PATH}/${id}/files${buildQuery(params)}`);
}

export function readContainerFile(id: number, params: ReadContainerFileParams) {
  return apiRequest<ReadContainerFileResponse>(`${SANDBOX_CONTAINERS_PATH}/${id}/files/read${buildQuery(params)}`);
}

export function writeContainerFile(id: number, payload: ContainerFileWriteRequest) {
  return apiRequest<ContainerFileWriteResponse>(`${SANDBOX_CONTAINERS_PATH}/${id}/files/write`, { method: "POST", body: payload });
}

export function uploadContainerFiles(
  id: number,
  path: ContainerFileUploadRequest["path"],
  files: File[],
  overwrite: ContainerFileUploadRequest["overwrite"] = true,
) {
  const form = new FormData();
  form.set("path", path);
  form.set("overwrite", String(overwrite));
  files.forEach((file) => form.append("files", file));
  return apiFormRequest<ContainerFileUploadResponse>(`${SANDBOX_CONTAINERS_PATH}/${id}/files/upload`, form);
}

export function downloadContainerFiles(id: number, params: DownloadContainerFilesParams) {
  const query = new URLSearchParams();
  params.path.forEach((path) => query.append("path", path));
  return apiBlobRequest(`${SANDBOX_CONTAINERS_PATH}/${id}/files/download?${query.toString()}`);
}

export function copyContainerFiles(id: number, payload: ContainerFileCopyRequest) {
  return apiRequest<ContainerFileCopyResponse>(`${SANDBOX_CONTAINERS_PATH}/${id}/files/copy`, { method: "POST", body: payload });
}

export function moveContainerFiles(id: number, payload: ContainerFileMoveRequest) {
  return apiRequest<ContainerFileMoveResponse>(`${SANDBOX_CONTAINERS_PATH}/${id}/files/move`, { method: "POST", body: payload });
}

export function deleteContainerFiles(id: number, payload: ContainerFileDeleteRequest) {
  return apiRequest<ContainerFileDeleteResponse>(`${SANDBOX_CONTAINERS_PATH}/${id}/files/delete`, { method: "POST", body: payload });
}

export function createContainerDirectory(id: number, payload: ContainerFileMkdirRequest) {
  return apiRequest<ContainerFileMkdirResponse>(`${SANDBOX_CONTAINERS_PATH}/${id}/files/mkdir`, { method: "POST", body: payload });
}

async function apiFormRequest<ResponsePayload>(path: string, body: FormData) {
  const headers = new Headers({ Accept: "application/json" });
  addAuthHeader(headers);

  let response: Response;
  try {
    response = await fetch(path, { method: "POST", headers, body });
  } catch (error) {
    throw new ApiError(0, { code: 0, message: error instanceof Error ? error.message : "Network request failed" });
  }

  const parsed = await parseJsonResponse(response);
  const payloadCode = typeof parsed?.code === "number" ? parsed.code : response.status;
  if (!response.ok || payloadCode >= 400) {
    handleAuthExpired(response.status, payloadCode);
    throw new ApiError(response.status, parsed);
  }
  return parsed as ResponsePayload;
}

async function apiBlobRequest(path: string) {
  const headers = new Headers();
  addAuthHeader(headers);

  let response: Response;
  try {
    response = await fetch(path, { headers });
  } catch (error) {
    throw new ApiError(0, { code: 0, message: error instanceof Error ? error.message : "Network request failed" });
  }

  if (!response.ok) {
    const parsed = await parseJsonResponse(response);
    const payloadCode = typeof parsed?.code === "number" ? parsed.code : response.status;
    handleAuthExpired(response.status, payloadCode);
    throw new ApiError(response.status, parsed);
  }

  const blob = await response.blob();
  const filename = parseContentDispositionFilename(response.headers.get("content-disposition"));
  return { blob, filename };
}

function addAuthHeader(headers: Headers) {
  const token = getStoredAccessToken();
  if (token) headers.set("Authorization", `Bearer ${token}`);
}

async function parseJsonResponse(response: Response) {
  const contentType = response.headers.get("content-type") || "";
  if (!contentType.includes("application/json")) return undefined;
  return response.json().catch(() => undefined);
}

function handleAuthExpired(status: number, payloadCode: number) {
  if (status !== 401 && payloadCode !== 401) return;
  clearStoredAccessToken();
  window.dispatchEvent(new Event("z3r0:auth-expired"));
}

function parseContentDispositionFilename(header: string | null) {
  if (!header) return "download";
  const encoded = /filename\*=UTF-8''([^;]+)/i.exec(header);
  if (encoded?.[1]) return decodeURIComponent(encoded[1]);
  const quoted = /filename="([^"]+)"/i.exec(header);
  if (quoted?.[1]) return quoted[1];
  return "download";
}
