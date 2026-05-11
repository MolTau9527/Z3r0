import asyncio
import json
from http import HTTPStatus
from typing import Any
from urllib.parse import quote

from fastapi import UploadFile, WebSocket, WebSocketDisconnect, status as ws_status
from fastapi.websockets import WebSocketState
from starlette.responses import JSONResponse
from starlette.responses import StreamingResponse

from logger import get_logger
from middleware.auth import decode_access_token
from schema.response_schema import CommonResponse
from schema.sandbox_container_schema import (
    ContainerFileCopyRequest,
    ContainerFileDeleteRequest,
    ContainerFileMkdirRequest,
    ContainerFileMoveRequest,
    ContainerFileReadResponse,
    ContainerFileUploadResponse,
    ContainerFileType,
    ContainerFileWriteRequest,
    CreateSandboxContainerRequest,
    DeleteSandboxContainerResponse,
    ListContainerFilesResponse,
    QuerySandboxContainersResponse,
    SandboxContainerDefaultPortMappingsResponse,
    SandboxContainerSchema,
    SandboxContainerStatus,
)
from schema.system_user_schema import SystemUserRole
from service.sandbox_container_service import (
    ContainerShellSession,
    SandboxContainerMutationResult,
    SandboxContainerRecord,
    ContainerUploadSource,
    copy_container_files,
    create_container_directory,
    create_sandbox_container,
    delete_container_files,
    delete_sandbox_container,
    download_container_paths,
    generate_default_sandbox_container_port_mappings,
    get_container_file_info,
    list_container_files,
    move_container_files,
    open_container_shell,
    query_available_sandbox_containers,
    query_sandbox_containers,
    read_container_file,
    read_container_shell,
    resize_container_shell,
    resolve_file_container,
    resolve_shell_container,
    start_sandbox_container,
    stop_sandbox_container,
    upload_container_files,
    write_container_file,
    write_container_shell,
)


logger = get_logger(__name__)


def _sandbox_container_schema(record: SandboxContainerRecord) -> SandboxContainerSchema:
    container = record.container
    return SandboxContainerSchema(
        id=container.id or 0,
        container_name=container.container_name,
        container_hash=container.container_hash,
        image_id=container.image_id,
        image_name=record.image_name,
        container_command=container.container_command,
        port_mappings=container.port_mappings,
        novnc_support=container.novnc_support,
        novnc_port=container.novnc_port,
        status=container.status,
        owner_id=container.owner_id,
        owner_username=record.owner_username,
        created_at=container.created_at,
        updated_at=container.updated_at,
    )


def _mutation_response(result: SandboxContainerMutationResult) -> CommonResponse:
    if result.record is None:
        status = HTTPStatus.NOT_FOUND if result.not_found else HTTPStatus.BAD_REQUEST
        return CommonResponse(code=status.value, message=result.message)
    if not result.changed:
        return CommonResponse(
            code=HTTPStatus.BAD_REQUEST.value,
            message=result.message,
            data=_sandbox_container_schema(result.record),
        )
    return CommonResponse(
        message=result.message,
        data=_sandbox_container_schema(result.record),
    )


async def create_sandbox_container_handler(
    request: CreateSandboxContainerRequest,
    owner_id: int,
) -> CommonResponse:
    result = await create_sandbox_container(
        image_id=request.image_id,
        owner_id=owner_id,
        container_command=request.container_command,
        port_mappings=request.port_mappings,
        novnc_support=request.novnc_support,
        novnc_port=request.novnc_port,
    )
    return _mutation_response(result)


async def generate_default_sandbox_container_port_mappings_handler(image_id: int) -> CommonResponse:
    result = await generate_default_sandbox_container_port_mappings(image_id=image_id)
    if not result.ok:
        status = HTTPStatus.NOT_FOUND if result.not_found else HTTPStatus.BAD_REQUEST
        return CommonResponse(code=status.value, message=result.message)
    return CommonResponse(
        message=result.message,
        data=SandboxContainerDefaultPortMappingsResponse(port_mappings=result.port_mappings),
    )


async def start_sandbox_container_handler(id: int) -> CommonResponse:
    return _mutation_response(await start_sandbox_container(id))


async def stop_sandbox_container_handler(id: int) -> CommonResponse:
    return _mutation_response(await stop_sandbox_container(id))


async def delete_sandbox_container_handler(id: int) -> CommonResponse:
    if not await delete_sandbox_container(id):
        return CommonResponse(code=HTTPStatus.NOT_FOUND.value, message="sandbox container not found")
    return CommonResponse(data=DeleteSandboxContainerResponse(id=id))


async def query_sandbox_containers_handler(page: int, size: int, keyword: str) -> CommonResponse:
    sandbox_containers = await query_sandbox_containers(page=page, size=size, keyword=keyword)
    return CommonResponse(data=QuerySandboxContainersResponse(
        page=page,
        size=size,
        items=[_sandbox_container_schema(record) for record in sandbox_containers],
    ))


async def query_available_sandbox_containers_handler(
    page: int,
    size: int,
    keyword: str,
    user_id: int,
    user_role: SystemUserRole,
) -> CommonResponse:
    sandbox_containers = await query_available_sandbox_containers(
        page=page,
        size=size,
        keyword=keyword,
        user_id=user_id,
        user_role=user_role,
    )
    return CommonResponse(data=QuerySandboxContainersResponse(
        page=page,
        size=size,
        items=[_sandbox_container_schema(record) for record in sandbox_containers],
    ))


async def handle_container_shell_stream(websocket: WebSocket, container_hash: str, token: str) -> None:
    if not _is_admin_ws_token(token):
        await websocket.close(code=ws_status.WS_1008_POLICY_VIOLATION)
        return

    sandbox_container = await resolve_shell_container(container_hash)
    if sandbox_container is None:
        await websocket.close(code=ws_status.WS_1008_POLICY_VIOLATION)
        return

    await websocket.accept()
    shell: ContainerShellSession | None = None
    reader: asyncio.Task | None = None
    receiver: asyncio.Task | None = None

    try:
        shell = await open_container_shell(container_hash)
        reader = asyncio.create_task(_forward_shell_output(websocket, shell))

        while True:
            receiver = asyncio.create_task(websocket.receive_text())
            done, _ = await asyncio.wait(
                {receiver, reader},
                return_when=asyncio.FIRST_COMPLETED,
            )
            if reader in done:
                await reader
                await _close_silently(websocket, ws_status.WS_1000_NORMAL_CLOSURE)
                return

            message = receiver.result()
            receiver = None
            try:
                payload = json.loads(message)
            except json.JSONDecodeError:
                await write_container_shell(shell, message)
                continue

            if not isinstance(payload, dict):
                continue
            message_type = payload.get("type")
            if message_type == "input":
                await write_container_shell(shell, str(payload.get("data", "")))
            elif message_type == "resize":
                rows = _bounded_int(payload.get("rows"), default=24, minimum=1, maximum=300)
                cols = _bounded_int(payload.get("cols"), default=80, minimum=1, maximum=500)
                await resize_container_shell(shell, rows=rows, cols=cols)
    except WebSocketDisconnect:
        pass
    except Exception:
        logger.exception("container shell stream failed: %s", container_hash)
        await _close_silently(websocket)
    finally:
        try:
            if shell is not None:
                shell.shutdown()
            await _cancel_task(receiver)
            await _finish_reader_task(reader)
        finally:
            if shell is not None:
                await asyncio.to_thread(shell.close)


async def _forward_shell_output(websocket: WebSocket, shell: ContainerShellSession) -> None:
    while True:
        data = await read_container_shell(shell)
        if not data:
            return
        if websocket.client_state != WebSocketState.CONNECTED or websocket.application_state != WebSocketState.CONNECTED:
            return
        await websocket.send_bytes(data)


async def _cancel_task(task: asyncio.Task | None) -> None:
    if task is None:
        return
    if task.done():
        try:
            task.result()
        except asyncio.CancelledError:
            pass
        except Exception:
            pass
        return
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


async def _finish_reader_task(task: asyncio.Task | None) -> None:
    if task is None:
        return
    try:
        await asyncio.wait_for(asyncio.shield(task), timeout=1)
    except asyncio.TimeoutError:
        await _cancel_task(task)
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.debug("container shell reader stopped with error", exc_info=True)


async def _close_silently(websocket: WebSocket, code: int = ws_status.WS_1011_INTERNAL_ERROR) -> None:
    try:
        await websocket.close(code=code)
    except Exception:
        pass


def _is_admin_ws_token(token: str) -> bool:
    try:
        user = decode_access_token(token)
    except Exception:
        return False
    return user is not None and user.role == SystemUserRole.ADMIN


def _bounded_int(value: Any, default: int, minimum: int, maximum: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return max(minimum, min(number, maximum))


# ── container file manager handlers ───────────────────────────────────────────


def _file_container_common_response(data: Any = None, message: str = "success") -> CommonResponse:
    return CommonResponse(data=data, message=message)


def _file_container_error(code: int, message: str) -> CommonResponse:
    return CommonResponse(code=code, message=message)


def _file_container_json_error(code: int, message: str) -> JSONResponse:
    return JSONResponse(
        status_code=code,
        content=_file_container_error(code, message).model_dump(),
    )


async def _resolve_running_container(id: int, action: str) -> tuple[str, CommonResponse | None]:
    """Resolve container and check it is running. Returns (hash, error_or_none)."""
    resolved = await resolve_file_container(id)
    if resolved is None:
        return "", _file_container_error(HTTPStatus.NOT_FOUND.value, "sandbox container not found")
    container_hash, status = resolved
    if status != SandboxContainerStatus.RUNNING:
        return "", _file_container_error(HTTPStatus.BAD_REQUEST.value, f"only running sandbox containers can {action}")
    return container_hash, None


async def handle_list_files(id: int, path: str) -> CommonResponse:
    container_hash, error = await _resolve_running_container(id, "browse files")
    if error:
        return error
    try:
        files = await list_container_files(container_hash, path)
    except Exception:
        logger.exception("failed to list container files: %s", id)
        return _file_container_error(HTTPStatus.INTERNAL_SERVER_ERROR.value, "failed to list container files")
    return _file_container_common_response(ListContainerFilesResponse(path=path, files=files))


async def handle_read_file(id: int, path: str, base64_mode: bool = False) -> CommonResponse:
    container_hash, error = await _resolve_running_container(id, "read files")
    if error:
        return error
    try:
        info = await get_container_file_info(container_hash, path)
        if info is None:
            return _file_container_error(HTTPStatus.NOT_FOUND.value, "file not found")
        if info.type == ContainerFileType.DIRECTORY:
            return _file_container_error(HTTPStatus.BAD_REQUEST.value, "cannot read a directory")
    except Exception:
        logger.exception("failed to get container file info: %s", id)
        return _file_container_error(HTTPStatus.INTERNAL_SERVER_ERROR.value, "failed to get container file info")
    try:
        content = await read_container_file(container_hash, path, base64_mode=base64_mode)
    except Exception:
        logger.exception("failed to read container file: %s", id)
        return _file_container_error(HTTPStatus.INTERNAL_SERVER_ERROR.value, "failed to read container file")
    return _file_container_common_response(ContainerFileReadResponse(path=path, content=content, size=len(content.encode())))


async def handle_write_file(id: int, body: ContainerFileWriteRequest) -> CommonResponse:
    container_hash, error = await _resolve_running_container(id, "write files")
    if error:
        return error
    try:
        ok = await write_container_file(container_hash, body.path, body.content)
    except Exception:
        logger.exception("failed to write container file: %s", id)
        return _file_container_error(HTTPStatus.INTERNAL_SERVER_ERROR.value, "failed to write container file")
    if not ok:
        return _file_container_error(HTTPStatus.INTERNAL_SERVER_ERROR.value, "failed to write container file")
    return _file_container_common_response(message="file written")


async def handle_upload_files(
    id: int,
    path: str,
    files: list[UploadFile],
    overwrite: bool,
) -> CommonResponse:
    container_hash, error = await _resolve_running_container(id, "upload files")
    if error:
        return error
    if not files:
        return _file_container_error(HTTPStatus.BAD_REQUEST.value, "no files uploaded")

    try:
        sources = [ContainerUploadSource(filename=file.filename or "", stream=file.file) for file in files]
        uploaded = await upload_container_files(container_hash, path, sources, overwrite)
    except ValueError as exc:
        return _file_container_error(HTTPStatus.BAD_REQUEST.value, str(exc))
    except FileExistsError as exc:
        return _file_container_error(HTTPStatus.CONFLICT.value, str(exc))
    except FileNotFoundError as exc:
        return _file_container_error(HTTPStatus.NOT_FOUND.value, str(exc))
    except Exception:
        logger.exception("failed to upload container files: %s", id)
        return _file_container_error(HTTPStatus.INTERNAL_SERVER_ERROR.value, "failed to upload container files")

    return _file_container_common_response(
        data=ContainerFileUploadResponse(path=path, files=uploaded),
        message="files uploaded",
    )


async def handle_download_files(id: int, paths: list[str]) -> StreamingResponse | JSONResponse:
    container_hash, error = await _resolve_running_container(id, "download files")
    if error:
        return JSONResponse(status_code=error.code, content=error.model_dump())
    if not paths:
        return _file_container_json_error(HTTPStatus.BAD_REQUEST.value, "download path is required")

    try:
        download = await download_container_paths(container_hash, paths)
    except ValueError as exc:
        return _file_container_json_error(HTTPStatus.BAD_REQUEST.value, str(exc))
    except FileNotFoundError as exc:
        return _file_container_json_error(HTTPStatus.NOT_FOUND.value, str(exc))
    except Exception:
        logger.exception("failed to download container files: %s", id)
        return _file_container_json_error(HTTPStatus.INTERNAL_SERVER_ERROR.value, "failed to download container files")

    filename = download.filename.replace('"', "_")
    encoded_filename = quote(download.filename)
    return StreamingResponse(
        download.chunks,
        media_type=download.media_type,
        headers={
            "Content-Disposition": f"attachment; filename=\"{filename}\"; filename*=UTF-8''{encoded_filename}",
            "X-Content-Type-Options": "nosniff",
        },
    )


async def handle_copy_files(id: int, body: ContainerFileCopyRequest) -> CommonResponse:
    container_hash, error = await _resolve_running_container(id, "copy files")
    if error:
        return error
    try:
        ok = await copy_container_files(container_hash, body.sources, body.destination)
    except Exception:
        logger.exception("failed to copy container files: %s", id)
        return _file_container_error(HTTPStatus.INTERNAL_SERVER_ERROR.value, "failed to copy container files")
    if not ok:
        return _file_container_error(HTTPStatus.INTERNAL_SERVER_ERROR.value, "failed to copy container files")
    return _file_container_common_response(message="files copied")


async def handle_move_files(id: int, body: ContainerFileMoveRequest) -> CommonResponse:
    container_hash, error = await _resolve_running_container(id, "move files")
    if error:
        return error
    try:
        ok = await move_container_files(container_hash, body.sources, body.destination)
    except Exception:
        logger.exception("failed to move container files: %s", id)
        return _file_container_error(HTTPStatus.INTERNAL_SERVER_ERROR.value, "failed to move container files")
    if not ok:
        return _file_container_error(HTTPStatus.INTERNAL_SERVER_ERROR.value, "failed to move container files")
    return _file_container_common_response(message="files moved")


async def handle_delete_files(id: int, body: ContainerFileDeleteRequest) -> CommonResponse:
    container_hash, error = await _resolve_running_container(id, "delete files")
    if error:
        return error
    try:
        ok = await delete_container_files(container_hash, body.paths)
    except Exception:
        logger.exception("failed to delete container files: %s", id)
        return _file_container_error(HTTPStatus.INTERNAL_SERVER_ERROR.value, "failed to delete container files")
    if not ok:
        return _file_container_error(HTTPStatus.INTERNAL_SERVER_ERROR.value, "failed to delete container files")
    return _file_container_common_response(message="files deleted")


async def handle_mkdir(id: int, body: ContainerFileMkdirRequest) -> CommonResponse:
    container_hash, error = await _resolve_running_container(id, "create directories")
    if error:
        return error
    try:
        ok = await create_container_directory(container_hash, body.path)
    except Exception:
        logger.exception("failed to create container directory: %s", id)
        return _file_container_error(HTTPStatus.INTERNAL_SERVER_ERROR.value, "failed to create container directory")
    if not ok:
        return _file_container_error(HTTPStatus.INTERNAL_SERVER_ERROR.value, "failed to create container directory")
    return _file_container_common_response(message="directory created")
