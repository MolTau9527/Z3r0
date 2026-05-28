import asyncio
import socket as py_socket
from dataclasses import dataclass

import docker
from docker.utils import socket as docker_socket
from sqlmodel import select

from database import get_async_session
from logger import get_logger
from model.sandbox.containers import SandboxContainer
from schema.sandbox.containers import SandboxContainerStatus
from service.sandbox import close_docker_response_sync as _close_shell_response_sync
from service.sandbox.docker_ops import (
    docker_status_to_sandbox_status,
    inspect_container_state_sync,
)
from service.sandbox.status import save_sandbox_container_status


logger = get_logger(__name__)

_SHELL_CANDIDATES = (("/bin/bash", "-l"), ("/bin/sh",))
_DEFAULT_SHELL_ROWS = 24
_DEFAULT_SHELL_COLS = 80


@dataclass
class ContainerShellSession:
    client: docker.DockerClient
    socket: object
    raw_socket: object
    response: object | None
    exec_id: str
    shutdown_started: bool = False
    closed: bool = False

    def shutdown(self) -> None:
        if self.shutdown_started:
            return
        self.shutdown_started = True

        if self.raw_socket is not self.socket:
            _shutdown_shell_socket_sync(self.raw_socket)
        _shutdown_shell_socket_sync(self.socket)

    def close(self) -> None:
        if self.closed:
            return
        self.closed = True

        try:
            self.shutdown()
            _close_shell_response_sync(self.socket, self.response)
            self.response = None
            if self.raw_socket is not self.socket:
                _close_shell_socket_sync(self.raw_socket)
            _close_shell_socket_sync(self.socket)
        finally:
            self.client.close()


async def resolve_shell_container(container_hash: str) -> SandboxContainer | None:
    async with get_async_session() as session:
        result = await session.exec(
            select(SandboxContainer).where(SandboxContainer.container_hash == container_hash)
        )
        sandbox_container = result.first()
        if sandbox_container is None or sandbox_container.status != SandboxContainerStatus.RUNNING:
            return None

    state = await asyncio.to_thread(inspect_container_state_sync, container_hash)
    status = SandboxContainerStatus.ERROR if not state.exists else docker_status_to_sandbox_status(state.status)
    if status != SandboxContainerStatus.RUNNING:
        if sandbox_container.id is not None:
            await save_sandbox_container_status(sandbox_container.id, status)
        return None

    return sandbox_container


async def open_container_shell(
    container_hash: str,
    rows: int = _DEFAULT_SHELL_ROWS,
    cols: int = _DEFAULT_SHELL_COLS,
) -> ContainerShellSession:
    return await asyncio.to_thread(_open_container_shell_sync, container_hash, rows, cols)


async def resize_container_shell(session: ContainerShellSession, rows: int, cols: int) -> None:
    rows = max(1, min(rows, 300))
    cols = max(1, min(cols, 500))
    await asyncio.to_thread(session.client.api.exec_resize, session.exec_id, height=rows, width=cols)


async def read_container_shell(session: ContainerShellSession) -> bytes:
    return await asyncio.to_thread(_read_shell_sync, session.raw_socket)


async def write_container_shell(session: ContainerShellSession, data: str) -> None:
    if not data:
        return
    await asyncio.to_thread(_write_shell_sync, session.raw_socket, data.encode())


def _open_container_shell_sync(container_hash: str, rows: int, cols: int) -> ContainerShellSession:
    client = docker.from_env()
    try:
        container = client.containers.get(container_hash)
        exec_id = _create_shell_exec(client, container.id)
        socket, response = _start_shell_exec_socket(client, exec_id)
        raw_socket = getattr(socket, "_sock", socket)
        try:
            client.api.exec_resize(exec_id, height=rows, width=cols)
        except Exception:
            logger.debug("failed to resize shell exec during open", exc_info=True)
        return ContainerShellSession(
            client=client,
            socket=socket,
            raw_socket=raw_socket,
            response=response,
            exec_id=exec_id,
        )
    except Exception:
        client.close()
        raise


def _start_shell_exec_socket(client: docker.DockerClient, exec_id: str) -> tuple[object, object | None]:
    api = client.api
    post_json = getattr(api, "_post_json", None)
    url = getattr(api, "_url", None)
    get_raw_response_socket = getattr(api, "_get_raw_response_socket", None)

    if callable(post_json) and callable(url) and callable(get_raw_response_socket):
        response = post_json(
            url(f"/exec/{exec_id}/start"),
            headers={"Connection": "Upgrade", "Upgrade": "tcp"},
            data={"Tty": True, "Detach": False},
            stream=True,
        )
        try:
            return get_raw_response_socket(response), response
        except Exception:
            _close_shell_response_sync(response, response)
            raise

    socket = api.exec_start(exec_id, tty=True, socket=True)
    return socket, getattr(socket, "_response", None)


def _create_shell_exec(client: docker.DockerClient, container_id: str) -> str:
    last_error: Exception | None = None
    for command in _SHELL_CANDIDATES:
        try:
            response = client.api.exec_create(
                container=container_id,
                cmd=list(command),
                stdin=True,
                stdout=True,
                stderr=True,
                tty=True,
                environment={"TERM": "xterm-256color"},
            )
            return str(response["Id"])
        except Exception as exc:
            last_error = exc
    raise RuntimeError("no supported shell found in container") from last_error


def _write_shell_sync(socket: object, data: bytes) -> None:
    if hasattr(socket, "sendall"):
        socket.sendall(data)
        return
    if hasattr(socket, "send"):
        socket.send(data)
        return
    if hasattr(socket, "write"):
        socket.write(data)
        flush = getattr(socket, "flush", None)
        if callable(flush):
            flush()
        return
    raise RuntimeError("docker exec socket is not writable")


def _read_shell_sync(socket: object) -> bytes:
    try:
        data = docker_socket.read(socket)
    except (OSError, ValueError):
        return b""
    if isinstance(data, str):
        return data.encode()
    return data or b""


def _shutdown_shell_socket_sync(socket: object) -> None:
    shutdown = getattr(socket, "shutdown", None)
    if callable(shutdown):
        try:
            shutdown(py_socket.SHUT_RDWR)
        except Exception:
            pass


def _close_shell_socket_sync(socket: object) -> None:
    _shutdown_shell_socket_sync(socket)

    close = getattr(socket, "close", None)
    if callable(close):
        try:
            close()
        except Exception:
            pass
