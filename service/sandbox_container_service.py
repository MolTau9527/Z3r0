import asyncio
import base64
import copy
import os
import posixpath
import secrets
import shlex
import shutil
import socket as py_socket
import stat as stat_module
import tarfile
import tempfile
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime
from typing import Any, BinaryIO, Literal
from uuid import uuid4

import docker
from docker.utils import socket as docker_socket
from sqlalchemy import String, cast, or_
from sqlmodel import select

from database import get_async_session
from logger import get_logger
from model.sandbox_container_model import SandboxContainer
from model.sandbox_image_model import SandboxImage
from model.system_user_model import SystemUser
from schema.sandbox_container_schema import (
    DEFAULT_SANDBOX_CONTAINER_COMMAND,
    ContainerFileInfo,
    ContainerFileUploadItem,
    ContainerFileType,
    SandboxContainerPortMapping,
    SandboxContainerStatus,
)
from schema.sandbox_image_schema import SandboxImageStatus
from schema.system_user_schema import SystemUserRole


logger = get_logger(__name__)

_SHELL_CANDIDATES = (("/bin/bash", "-l"), ("/bin/sh",))
_DEFAULT_SHELL_ROWS = 24
_DEFAULT_SHELL_COLS = 80
_STATUS_MONITOR_INTERVAL_SECONDS = 5
_TOOL_BINDING_INSPECT_TTL_SECONDS = 3
_COMMAND_CANCEL_JOIN_TIMEOUT_SECONDS = 3
_COMMAND_TERMINATE_TIMEOUT_SECONDS = 5
_RANDOM_HOST_PORT_MIN = 49152
_RANDOM_HOST_PORT_MAX = 65535
_RANDOM_HOST_PORT_ATTEMPTS = 128
_ARCHIVE_CHUNK_SIZE = 1024 * 1024
_UPLOAD_ARCHIVE_MEMORY_LIMIT = 16 * 1024 * 1024
_DOWNLOAD_ARCHIVE_MEMORY_LIMIT = 64 * 1024 * 1024
_status_monitor_task: asyncio.Task[None] | None = None
_tool_binding_state_cache: dict[int, "_DockerStateCacheEntry"] = {}


SandboxContainerProtocol = Literal["tcp", "udp"]


@dataclass(frozen=True)
class _ExposedPort:
    container_port: int
    protocol: SandboxContainerProtocol


@dataclass(frozen=True)
class SandboxContainerRecord:
    container: SandboxContainer
    image_name: str
    owner_username: str


@dataclass(frozen=True)
class SandboxContainerMutationResult:
    record: SandboxContainerRecord | None
    changed: bool
    message: str = ""
    not_found: bool = False


@dataclass(frozen=True)
class SandboxContainerDefaultPortMappingsResult:
    port_mappings: list[SandboxContainerPortMapping]
    ok: bool
    message: str = ""
    not_found: bool = False


@dataclass(frozen=True)
class SandboxContainerCommandResult:
    output: str
    exit_code: int


@dataclass(frozen=True)
class SandboxContainerToolBinding:
    id: int
    generation: int


class _SandboxCommandCancelled(RuntimeError):
    pass


@dataclass(frozen=True)
class _ContainerStatusSnapshot:
    id: int
    container_hash: str
    status: SandboxContainerStatus


@dataclass(frozen=True)
class _DockerContainerState:
    exists: bool
    status: str = ""


@dataclass(frozen=True)
class _DockerStateCacheEntry:
    container_hash: str
    generation: int
    state: _DockerContainerState
    expires_at: float


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


@dataclass(frozen=True)
class ContainerUploadSource:
    filename: str
    stream: BinaryIO


@dataclass(frozen=True)
class ContainerDownloadStream:
    filename: str
    media_type: str
    chunks: Iterator[bytes]


def _generate_container_name(image_id: int) -> str:
    return f"sandbox-{image_id}-{uuid4().hex[:12]}"


def _image_ref(image: SandboxImage) -> str:
    if image.image_hash:
        return f"sha256:{image.image_hash}"
    return image.image_name


def _serialize_port_mappings(port_mappings: list[SandboxContainerPortMapping]) -> list[dict]:
    return [mapping.model_dump() for mapping in port_mappings]


def _to_docker_ports(port_mappings: list[SandboxContainerPortMapping]) -> dict[str, int] | None:
    if not port_mappings:
        return None
    return {
        f"{mapping.container_port}/{mapping.protocol}": mapping.host_port
        for mapping in port_mappings
    }


def _parse_exposed_ports(exposed_ports: Any) -> list[_ExposedPort]:
    if not isinstance(exposed_ports, dict):
        return []

    parsed: set[tuple[int, SandboxContainerProtocol]] = set()
    for raw_port in exposed_ports:
        if not isinstance(raw_port, str) or "/" not in raw_port:
            continue
        port_text, protocol = raw_port.rsplit("/", 1)
        if protocol not in {"tcp", "udp"}:
            continue
        try:
            container_port = int(port_text)
        except ValueError:
            continue
        if 1 <= container_port <= 65535:
            parsed.add((container_port, protocol))

    return [
        _ExposedPort(container_port=container_port, protocol=protocol)
        for container_port, protocol in sorted(parsed, key=lambda item: (item[0], item[1]))
    ]


def _inspect_image_exposed_ports_sync(image_ref: str) -> list[_ExposedPort]:
    client = docker.from_env()
    try:
        attrs = client.api.inspect_image(image_ref)
        config = attrs.get("Config")
        if not isinstance(config, dict):
            return []
        return _parse_exposed_ports(config.get("ExposedPorts"))
    finally:
        client.close()


def _create_container_sync(
    image_ref: str,
    container_name: str,
    container_command: str,
    port_mappings: list[SandboxContainerPortMapping],
) -> str:
    client = docker.from_env()
    try:
        create_kwargs = {
            "image": image_ref,
            "name": container_name,
            "ports": _to_docker_ports(port_mappings),
            "stdin_open": True,
            "tty": False,
        }
        if container_command:
            create_kwargs["entrypoint"] = ["/bin/sh", "-lc"]
            create_kwargs["command"] = [container_command]

        container = client.containers.create(
            **create_kwargs,
        )
        return container.id
    finally:
        client.close()


def _inspect_container_state_sync(container_hash: str) -> _DockerContainerState:
    client = docker.from_env()
    try:
        container = client.containers.get(container_hash)
        container.reload()
        return _DockerContainerState(exists=True, status=str(container.status or ""))
    except docker.errors.NotFound:
        return _DockerContainerState(exists=False)
    finally:
        client.close()


def _start_container_sync(container_hash: str) -> None:
    client = docker.from_env()
    try:
        container = client.containers.get(container_hash)
        container.start()
    finally:
        client.close()


def _stop_container_sync(container_hash: str) -> None:
    client = docker.from_env()
    try:
        container = client.containers.get(container_hash)
        container.stop()
    finally:
        client.close()


def _remove_container_sync(container_hash: str) -> None:
    client = docker.from_env()
    try:
        container = client.containers.get(container_hash)
        container.remove(force=True)
    except docker.errors.NotFound:
        logger.info("sandbox container instance already absent: %s", container_hash)
    finally:
        client.close()


class _RunningContainerCommand:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._stream: object | None = None
        self._closed = False

    def set_stream(self, stream: object) -> None:
        with self._lock:
            if self._closed:
                close_now = True
            else:
                self._stream = stream
                close_now = False
        if close_now:
            _close_command_stream(stream)

    def close(self) -> None:
        with self._lock:
            self._closed = True
            stream, self._stream = self._stream, None
        if stream is not None:
            _close_command_stream(stream)


async def _execute_container_command(
    container_hash: str,
    command: str,
) -> SandboxContainerCommandResult:
    marker_path = f"/tmp/z3r0-command-{uuid4().hex}.pid"
    cancel_requested = threading.Event()
    running_command = _RunningContainerCommand()
    command_task = asyncio.create_task(
        asyncio.to_thread(
            _execute_container_command_sync,
            container_hash,
            command,
            marker_path,
            cancel_requested,
            running_command,
        ),
        name="sandbox-container-command",
    )
    try:
        return await asyncio.shield(command_task)
    except asyncio.CancelledError:
        cancel_requested.set()
        running_command.close()
        await _terminate_container_command(container_hash, marker_path)
        await _drain_cancelled_command_task(command_task, container_hash)
        raise


async def _terminate_container_command(container_hash: str, marker_path: str) -> None:
    terminate_task = asyncio.create_task(
        asyncio.to_thread(_terminate_container_command_sync, container_hash, marker_path),
        name="sandbox-container-command-terminate",
    )
    try:
        await asyncio.wait_for(asyncio.shield(terminate_task), timeout=_COMMAND_TERMINATE_TIMEOUT_SECONDS + 1)
    except asyncio.TimeoutError:
        logger.warning("sandbox container command termination timed out: %s", container_hash)
        _consume_background_task(terminate_task)
    except docker.errors.NotFound:
        logger.info("sandbox container absent while cancelling command: %s", container_hash)
    except asyncio.CancelledError:
        _consume_background_task(terminate_task)
        raise
    except Exception:
        logger.warning("sandbox container command termination failed: %s", container_hash, exc_info=True)


async def _drain_cancelled_command_task(task: asyncio.Task, container_hash: str) -> None:
    if task.done():
        _consume_background_task(task)
        return
    try:
        await asyncio.wait_for(asyncio.shield(task), timeout=_COMMAND_CANCEL_JOIN_TIMEOUT_SECONDS)
    except asyncio.TimeoutError:
        logger.warning("sandbox container command did not exit after cancellation: %s", container_hash)
        _consume_background_task(task)
    except asyncio.CancelledError:
        _consume_background_task(task)
        raise
    except _SandboxCommandCancelled:
        pass
    except Exception:
        logger.debug("sandbox container command exited after cancellation with an error", exc_info=True)


def _consume_background_task(task: asyncio.Task) -> None:
    if task.done():
        _discard_background_task_result(task)
        return
    task.add_done_callback(_discard_background_task_result)


def _discard_background_task_result(task: asyncio.Task) -> None:
    try:
        task.result()
    except asyncio.CancelledError:
        pass
    except _SandboxCommandCancelled:
        pass
    except Exception:
        logger.debug("background sandbox command task failed", exc_info=True)


def _execute_container_command_sync(
    container_hash: str,
    command: str,
    marker_path: str,
    cancel_requested: threading.Event,
    running_command: _RunningContainerCommand,
) -> SandboxContainerCommandResult:
    client = docker.from_env()
    stream: object | None = None
    try:
        if cancel_requested.is_set():
            raise _SandboxCommandCancelled()
        container = client.containers.get(container_hash)
        exec_response = client.api.exec_create(
            container=container.id,
            cmd=["/bin/sh", "-lc", _wrap_cancellable_command(command, marker_path)],
            stdout=True,
            stderr=True,
            stdin=False,
            tty=False,
        )
        exec_id = str(exec_response["Id"])
        if cancel_requested.is_set():
            raise _SandboxCommandCancelled()
        stream = client.api.exec_start(exec_id, stream=True, demux=True)
        running_command.set_stream(stream)

        stdout_parts: list[bytes | str] = []
        stderr_parts: list[bytes | str] = []
        try:
            for chunk in stream:
                if cancel_requested.is_set():
                    raise _SandboxCommandCancelled()
                stdout, stderr = _split_command_output_chunk(chunk)
                if stdout:
                    stdout_parts.append(stdout)
                if stderr:
                    stderr_parts.append(stderr)
        except Exception:
            if cancel_requested.is_set():
                raise _SandboxCommandCancelled()
            raise
        if cancel_requested.is_set():
            raise _SandboxCommandCancelled()

        inspect_result = client.api.exec_inspect(exec_id)
        exit_code = inspect_result.get("ExitCode")
        return SandboxContainerCommandResult(
            output=_decode_command_output_parts(stdout_parts) + _decode_command_output_parts(stderr_parts),
            exit_code=exit_code if isinstance(exit_code, int) else 1,
        )
    finally:
        running_command.close()
        client.close()


def _terminate_container_command_sync(container_hash: str, marker_path: str) -> None:
    client = docker.from_env(timeout=_COMMAND_TERMINATE_TIMEOUT_SECONDS)
    try:
        container = client.containers.get(container_hash)
        container.exec_run(
            cmd=["/bin/sh", "-lc", _build_command_termination_script(marker_path)],
            stdout=True,
            stderr=True,
            stdin=False,
            tty=False,
            demux=True,
        )
    finally:
        client.close()


def _close_command_stream(stream: object) -> None:
    response = getattr(stream, "_response", None)
    close = getattr(stream, "close", None)
    if callable(close):
        try:
            close()
        except Exception:
            logger.debug("failed to close sandbox command stream", exc_info=True)
    _close_shell_response_sync(stream, response)
    try:
        setattr(stream, "_response", None)
    except Exception:
        pass


def _split_command_output_chunk(chunk: object) -> tuple[bytes | str | None, bytes | str | None]:
    if isinstance(chunk, tuple):
        stdout = chunk[0] if len(chunk) > 0 else None
        stderr = chunk[1] if len(chunk) > 1 else None
        return stdout, stderr
    if isinstance(chunk, (bytes, str)):
        return chunk, None
    return None, None


def _decode_command_output_parts(parts: list[bytes | str]) -> str:
    return "".join(_decode_command_output(part) for part in parts)


def _wrap_cancellable_command(command: str, marker_path: str) -> str:
    marker = shlex.quote(marker_path)
    quoted_command = shlex.quote(command)
    group_inner = (
        f"rm -f {marker}; "
        f"printf '%s' \"$$\" > {marker}; "
        f"/bin/sh -lc {quoted_command} & "
        "pid=$!; wait \"$pid\"; code=$?; "
        f"rm -f {marker}; "
        "exit \"$code\""
    )
    child_inner = (
        f"rm -f {marker}; "
        f"/bin/sh -lc {quoted_command} & "
        "pid=$!; "
        f"printf '%s' \"$pid\" > {marker}; "
        "wait \"$pid\"; code=$?; "
        f"rm -f {marker}; "
        "exit \"$code\""
    )
    return (
        "if command -v setsid >/dev/null 2>&1 "
        "&& setsid -w /bin/sh -lc 'exit 0' >/dev/null 2>&1; then "
        f"exec setsid -w /bin/sh -lc {shlex.quote(group_inner)}; "
        "else "
        f"exec /bin/sh -lc {shlex.quote(child_inner)}; "
        "fi"
    )


def _build_command_termination_script(marker_path: str) -> str:
    marker = shlex.quote(marker_path)
    return (
        "pid=''; "
        "for _ in 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15 16 17 18 19 20; do "
        f"pid=$(cat {marker} 2>/dev/null || true); "
        "[ -n \"$pid\" ] && break; "
        "sleep 0.1; "
        "done; "
        f"rm -f {marker}; "
        "if [ -n \"$pid\" ]; then "
        "kill -TERM -\"$pid\" 2>/dev/null || kill -TERM \"$pid\" 2>/dev/null || true; "
        "sleep 0.5; "
        "kill -KILL -\"$pid\" 2>/dev/null || kill -KILL \"$pid\" 2>/dev/null || true; "
        "fi"
    )


def _decode_command_output(output: bytes | str | None) -> str:
    if output is None:
        return ""
    if isinstance(output, bytes):
        return output.decode(errors="replace")
    return output


async def _load_sandbox_container_record(id: int) -> SandboxContainerRecord | None:
    statement = (
        select(SandboxContainer, SandboxImage.image_name, SystemUser.username)
        .join(SandboxImage, SandboxContainer.image_id == SandboxImage.id)
        .join(SystemUser, SandboxContainer.owner_id == SystemUser.id)
        .where(SandboxContainer.id == id)
    )

    async with get_async_session() as session:
        result = await session.exec(statement)
        row = result.first()
        if row is None:
            return None
        return SandboxContainerRecord(container=row[0], image_name=row[1], owner_username=row[2])


def _is_host_port_available(host_port: int, protocol: SandboxContainerProtocol) -> bool:
    socket_type = py_socket.SOCK_STREAM if protocol == "tcp" else py_socket.SOCK_DGRAM
    with py_socket.socket(py_socket.AF_INET, socket_type) as sock:
        try:
            sock.bind(("0.0.0.0", host_port))
        except OSError:
            return False
    return True


def _random_host_port(
    protocol: SandboxContainerProtocol,
    reserved: set[tuple[int, SandboxContainerProtocol]],
) -> int:
    port_count = _RANDOM_HOST_PORT_MAX - _RANDOM_HOST_PORT_MIN + 1
    for _ in range(_RANDOM_HOST_PORT_ATTEMPTS):
        host_port = _RANDOM_HOST_PORT_MIN + secrets.randbelow(port_count)
        key = (host_port, protocol)
        if key in reserved:
            continue
        if _is_host_port_available(host_port, protocol):
            reserved.add(key)
            return host_port
    raise RuntimeError("failed to allocate host port")


async def _load_reserved_host_ports() -> set[tuple[int, SandboxContainerProtocol]]:
    async with get_async_session() as session:
        result = await session.exec(select(SandboxContainer.port_mappings))
        rows = result.all()

    reserved: set[tuple[int, SandboxContainerProtocol]] = set()
    for mappings in rows:
        if not isinstance(mappings, list):
            continue
        for mapping in mappings:
            if not isinstance(mapping, dict):
                continue
            host_port = mapping.get("host_port")
            protocol = mapping.get("protocol")
            if isinstance(host_port, int) and protocol in {"tcp", "udp"}:
                reserved.add((host_port, protocol))
    return reserved


async def _save_sandbox_container_status(
    id: int,
    status: SandboxContainerStatus,
) -> SandboxContainerRecord | None:
    async with get_async_session() as session:
        sandbox_container = await session.get(SandboxContainer, id)
        if sandbox_container is None:
            return None

        sandbox_container.status = status
        sandbox_container.updated_at = datetime.now()
        session.add(sandbox_container)
        await session.commit()

    _schedule_agent_tool_invalidation(id)
    return await _load_sandbox_container_record(id)


def _status_generation(sandbox_container: SandboxContainer) -> int:
    return int(sandbox_container.updated_at.timestamp() * 1_000_000)


def _clear_tool_binding_state_cache(container_id: int | None = None) -> None:
    if container_id is None:
        _tool_binding_state_cache.clear()
        return
    _tool_binding_state_cache.pop(container_id, None)


async def _inspect_container_state_cached(
    *,
    id: int,
    container_hash: str,
    generation: int,
) -> _DockerContainerState:
    now = time.monotonic()
    cached = _tool_binding_state_cache.get(id)
    if (
        cached is not None
        and cached.container_hash == container_hash
        and cached.generation == generation
        and cached.expires_at > now
    ):
        return cached.state

    state = await asyncio.to_thread(_inspect_container_state_sync, container_hash)
    _tool_binding_state_cache[id] = _DockerStateCacheEntry(
        container_hash=container_hash,
        generation=generation,
        state=state,
        expires_at=now + _TOOL_BINDING_INSPECT_TTL_SECONDS,
    )
    return state


def _schedule_agent_tool_invalidation(container_id: int) -> None:
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        logger.debug("no running loop for agent tool invalidation: %s", container_id)
        return

    loop.create_task(
        _invalidate_agent_tool_bindings(container_id),
        name=f"agent-tool-invalidate-{container_id}",
    )


async def _invalidate_agent_tool_bindings(container_id: int) -> None:
    _clear_tool_binding_state_cache(container_id)
    try:
        from core.runtime import get_agent_pool

        await get_agent_pool().invalidate_tool_bindings(container_id)
    except Exception:
        logger.exception("agent tool binding invalidation failed: %s", container_id)


async def _invalidate_all_agent_tool_bindings() -> None:
    _clear_tool_binding_state_cache()
    try:
        from core.runtime import get_agent_pool

        await get_agent_pool().invalidate_tool_bindings()
    except Exception:
        logger.exception("agent tool binding invalidation failed")


def _docker_status_to_sandbox_status(status: str) -> SandboxContainerStatus:
    normalized = status.strip().lower()
    if normalized == "running":
        return SandboxContainerStatus.RUNNING
    if normalized == "created":
        return SandboxContainerStatus.CREATED
    if normalized == "exited":
        return SandboxContainerStatus.STOPPED
    return SandboxContainerStatus.ERROR


async def _load_container_status_snapshots() -> list[_ContainerStatusSnapshot]:
    statement = select(SandboxContainer.id, SandboxContainer.container_hash, SandboxContainer.status).where(
        SandboxContainer.container_hash != ""
    )
    async with get_async_session() as session:
        result = await session.exec(statement)
        return [
            _ContainerStatusSnapshot(id=row[0], container_hash=row[1], status=row[2])
            for row in result.all()
        ]


async def _sync_container_status(snapshot: _ContainerStatusSnapshot) -> None:
    state = await asyncio.to_thread(_inspect_container_state_sync, snapshot.container_hash)
    next_status = SandboxContainerStatus.ERROR if not state.exists else _docker_status_to_sandbox_status(state.status)
    if next_status == snapshot.status:
        return

    await _save_sandbox_container_status(snapshot.id, next_status)
    logger.info(
        "sandbox container status synced: %s %s -> %s",
        snapshot.id,
        snapshot.status,
        next_status,
    )


async def sync_sandbox_container_statuses() -> None:
    snapshots = await _load_container_status_snapshots()
    for snapshot in snapshots:
        try:
            await _sync_container_status(snapshot)
        except Exception:
            logger.exception("sandbox container status sync failed: %s", snapshot.id)


async def _status_monitor_loop() -> None:
    while True:
        try:
            await sync_sandbox_container_statuses()
            await asyncio.sleep(_STATUS_MONITOR_INTERVAL_SECONDS)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("sandbox container status monitor iteration failed")
            await asyncio.sleep(_STATUS_MONITOR_INTERVAL_SECONDS)


async def start_sandbox_container_status_monitor() -> None:
    global _status_monitor_task
    if _status_monitor_task is not None and not _status_monitor_task.done():
        return
    _status_monitor_task = asyncio.create_task(
        _status_monitor_loop(),
        name="sandbox-container-status-monitor",
    )
    logger.info("sandbox container status monitor started")


async def stop_sandbox_container_status_monitor() -> None:
    global _status_monitor_task
    task, _status_monitor_task = _status_monitor_task, None
    if task is None or task.done():
        return
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    logger.info("sandbox container status monitor stopped")


async def invalidate_all_agent_tool_bindings() -> None:
    await _invalidate_all_agent_tool_bindings()


async def create_sandbox_container(
    image_id: int,
    owner_id: int,
    port_mappings: list[SandboxContainerPortMapping],
    novnc_support: bool = False,
    novnc_port: int = 0,
    container_command: str = DEFAULT_SANDBOX_CONTAINER_COMMAND,
) -> SandboxContainerMutationResult:
    container_command = container_command.strip()
    novnc_port = novnc_port if novnc_support else 0

    async with get_async_session() as session:
        sandbox_image = await session.get(SandboxImage, image_id)
        if sandbox_image is None:
            return SandboxContainerMutationResult(
                record=None,
                changed=False,
                message="sandbox image not found",
                not_found=True,
            )
        if sandbox_image.status != SandboxImageStatus.READY:
            return SandboxContainerMutationResult(
                record=None,
                changed=False,
                message="only ready sandbox images can create containers",
            )

        owner = await session.get(SystemUser, owner_id)
        if owner is None:
            return SandboxContainerMutationResult(
                record=None,
                changed=False,
                message="system user not found",
                not_found=True,
            )

        image_ref = _image_ref(sandbox_image)

    container_name = _generate_container_name(image_id)
    try:
        container_hash = await asyncio.to_thread(
            _create_container_sync,
            image_ref,
            container_name,
            container_command,
            port_mappings,
        )
    except Exception:
        logger.exception("sandbox container create failed for image: %s", image_id)
        return SandboxContainerMutationResult(
            record=None,
            changed=False,
            message="failed to create sandbox container",
        )

    now = datetime.now()
    sandbox_container = SandboxContainer(
        container_name=container_name,
        container_hash=container_hash,
        container_command=container_command,
        owner_id=owner_id,
        image_id=image_id,
        port_mappings=_serialize_port_mappings(port_mappings),
        novnc_support=novnc_support,
        novnc_port=novnc_port,
        status=SandboxContainerStatus.CREATED,
        created_at=now,
        updated_at=now,
    )

    try:
        async with get_async_session() as session:
            session.add(sandbox_container)
            await session.commit()
            await session.refresh(sandbox_container)
    except Exception:
        await asyncio.to_thread(_remove_container_sync, container_hash)
        raise

    if sandbox_container.id is None:
        await asyncio.to_thread(_remove_container_sync, container_hash)
        raise RuntimeError("sandbox container id was not generated")

    logger.info("sandbox container created: %s", sandbox_container.id)
    return SandboxContainerMutationResult(
        record=await _load_sandbox_container_record(sandbox_container.id),
        changed=True,
        message="sandbox container created",
    )


async def generate_default_sandbox_container_port_mappings(
    image_id: int,
) -> SandboxContainerDefaultPortMappingsResult:
    async with get_async_session() as session:
        sandbox_image = await session.get(SandboxImage, image_id)
        if sandbox_image is None:
            return SandboxContainerDefaultPortMappingsResult(
                port_mappings=[],
                ok=False,
                message="sandbox image not found",
                not_found=True,
            )
        if sandbox_image.status != SandboxImageStatus.READY:
            return SandboxContainerDefaultPortMappingsResult(
                port_mappings=[],
                ok=False,
                message="only ready sandbox images can generate port mappings",
            )
        image_ref = _image_ref(sandbox_image)

    try:
        exposed_ports = await asyncio.to_thread(_inspect_image_exposed_ports_sync, image_ref)
    except Exception:
        logger.exception("sandbox image exposed ports inspect failed: %s", image_id)
        return SandboxContainerDefaultPortMappingsResult(
            port_mappings=[],
            ok=False,
            message="failed to inspect sandbox image exposed ports",
        )

    if not exposed_ports:
        return SandboxContainerDefaultPortMappingsResult(
            port_mappings=[],
            ok=True,
            message="sandbox image has no exposed ports",
        )

    reserved = await _load_reserved_host_ports()
    try:
        port_mappings = [
            SandboxContainerPortMapping(
                container_port=exposed.container_port,
                host_port=_random_host_port(exposed.protocol, reserved),
                protocol=exposed.protocol,
            )
            for exposed in exposed_ports
        ]
    except RuntimeError:
        logger.exception("sandbox container host port allocation failed for image: %s", image_id)
        return SandboxContainerDefaultPortMappingsResult(
            port_mappings=[],
            ok=False,
            message="failed to allocate host ports",
        )

    return SandboxContainerDefaultPortMappingsResult(
        port_mappings=port_mappings,
        ok=True,
        message="sandbox container port mappings generated",
    )


async def start_sandbox_container(id: int) -> SandboxContainerMutationResult:
    record = await _load_sandbox_container_record(id)
    if record is None:
        return SandboxContainerMutationResult(
            record=None,
            changed=False,
            message="sandbox container not found",
            not_found=True,
        )
    if record.container.status not in {SandboxContainerStatus.CREATED, SandboxContainerStatus.STOPPED}:
        return SandboxContainerMutationResult(
            record=record,
            changed=False,
            message="only created or stopped sandbox containers can be started",
        )

    try:
        await asyncio.to_thread(_start_container_sync, record.container.container_hash)
        await asyncio.sleep(1)
        await _sync_container_status(_ContainerStatusSnapshot(
            id=record.container.id or id,
            container_hash=record.container.container_hash,
            status=record.container.status,
        ))
    except docker.errors.NotFound:
        logger.info("sandbox container instance not found while starting: %s", id)
        return SandboxContainerMutationResult(
            record=await _save_sandbox_container_status(id, SandboxContainerStatus.ERROR),
            changed=False,
            message="sandbox container instance not found",
        )
    except Exception:
        logger.exception("sandbox container start failed: %s", id)
        return SandboxContainerMutationResult(
            record=await _save_sandbox_container_status(id, SandboxContainerStatus.ERROR),
            changed=False,
            message="failed to start sandbox container",
        )

    next_record = await _load_sandbox_container_record(id)
    if next_record is not None and next_record.container.status == SandboxContainerStatus.RUNNING:
        logger.info("sandbox container started: %s", id)
        return SandboxContainerMutationResult(
            record=next_record,
            changed=True,
            message="sandbox container started",
        )

    logger.info("sandbox container exited after start: %s", id)
    return SandboxContainerMutationResult(
        record=next_record,
        changed=False,
        message="sandbox container is not running after start",
    )


async def stop_sandbox_container(id: int) -> SandboxContainerMutationResult:
    record = await _load_sandbox_container_record(id)
    if record is None:
        return SandboxContainerMutationResult(
            record=None,
            changed=False,
            message="sandbox container not found",
            not_found=True,
        )
    if record.container.status != SandboxContainerStatus.RUNNING:
        return SandboxContainerMutationResult(
            record=record,
            changed=False,
            message="only running sandbox containers can be stopped",
        )

    try:
        await asyncio.to_thread(_stop_container_sync, record.container.container_hash)
    except docker.errors.NotFound:
        logger.info("sandbox container instance not found while stopping: %s", id)
        return SandboxContainerMutationResult(
            record=await _save_sandbox_container_status(id, SandboxContainerStatus.ERROR),
            changed=False,
            message="sandbox container instance not found",
        )
    except Exception:
        logger.exception("sandbox container stop failed: %s", id)
        return SandboxContainerMutationResult(
            record=await _save_sandbox_container_status(id, SandboxContainerStatus.ERROR),
            changed=False,
            message="failed to stop sandbox container",
        )

    logger.info("sandbox container stopped: %s", id)
    return SandboxContainerMutationResult(
        record=await _save_sandbox_container_status(id, SandboxContainerStatus.STOPPED),
        changed=True,
        message="sandbox container stopped",
    )


async def delete_sandbox_container(id: int) -> bool:
    async with get_async_session() as session:
        sandbox_container = await session.get(SandboxContainer, id)
        if sandbox_container is None:
            return False

        await asyncio.to_thread(_remove_container_sync, sandbox_container.container_hash)
        await session.delete(sandbox_container)
        await session.commit()

    await _invalidate_agent_tool_bindings(id)
    logger.info("sandbox container deleted: %s", id)
    return True


async def query_sandbox_containers(
    page: int = 1,
    size: int = 100,
    keyword: str = "",
) -> list[SandboxContainerRecord]:
    statement = (
        select(SandboxContainer, SandboxImage.image_name, SystemUser.username)
        .join(SandboxImage, SandboxContainer.image_id == SandboxImage.id)
        .join(SystemUser, SandboxContainer.owner_id == SystemUser.id)
        .order_by(SandboxContainer.id)
        .offset((page - 1) * size)
        .limit(size)
    )

    keyword = keyword.strip()
    if keyword:
        pattern = f"%{keyword}%"
        statement = statement.where(
            or_(
                SandboxContainer.container_name.ilike(pattern),
                SandboxContainer.container_hash.ilike(pattern),
                SandboxImage.image_name.ilike(pattern),
                SystemUser.username.ilike(pattern),
                cast(SandboxContainer.status, String).ilike(pattern),
                cast(SandboxContainer.port_mappings, String).ilike(pattern),
            )
        )

    async with get_async_session() as session:
        result = await session.exec(statement)
        return [
            SandboxContainerRecord(container=row[0], image_name=row[1], owner_username=row[2])
            for row in result.all()
        ]


async def query_available_sandbox_containers(
    user_id: int,
    user_role: SystemUserRole,
    page: int = 1,
    size: int = 100,
    keyword: str = "",
) -> list[SandboxContainerRecord]:
    statement = (
        select(SandboxContainer, SandboxImage.image_name, SystemUser.username)
        .join(SandboxImage, SandboxContainer.image_id == SandboxImage.id)
        .join(SystemUser, SandboxContainer.owner_id == SystemUser.id)
        .order_by(SandboxContainer.id)
        .offset((page - 1) * size)
        .limit(size)
    )

    if user_role != SystemUserRole.ADMIN:
        statement = statement.where(SandboxContainer.owner_id == user_id)
    statement = statement.where(SandboxContainer.status == SandboxContainerStatus.RUNNING)

    keyword = keyword.strip()
    if keyword:
        pattern = f"%{keyword}%"
        statement = statement.where(
            or_(
                SandboxContainer.container_name.ilike(pattern),
                SandboxContainer.container_hash.ilike(pattern),
                SandboxImage.image_name.ilike(pattern),
                SystemUser.username.ilike(pattern),
                cast(SandboxContainer.status, String).ilike(pattern),
                cast(SandboxContainer.port_mappings, String).ilike(pattern),
            )
        )

    async with get_async_session() as session:
        result = await session.exec(statement)
        return [
            SandboxContainerRecord(container=row[0], image_name=row[1], owner_username=row[2])
            for row in result.all()
        ]


async def resolve_sandbox_container_tool_binding(
    id: int,
    user_id: int,
    user_role: SystemUserRole,
) -> SandboxContainerToolBinding | None:
    async with get_async_session() as session:
        sandbox_container = await session.get(SandboxContainer, id)
        if sandbox_container is None:
            return None
        if sandbox_container.status != SandboxContainerStatus.RUNNING:
            return None
        if user_role != SystemUserRole.ADMIN and sandbox_container.owner_id != user_id:
            return None
        container_hash = sandbox_container.container_hash
        generation = _status_generation(sandbox_container)

    try:
        state = await _inspect_container_state_cached(
            id=id,
            container_hash=container_hash,
            generation=generation,
        )
    except Exception:
        logger.exception("sandbox container inspect failed before tool binding: %s", id)
        return None

    status = SandboxContainerStatus.ERROR if not state.exists else _docker_status_to_sandbox_status(state.status)
    if status != SandboxContainerStatus.RUNNING:
        await _save_sandbox_container_status(id, status)
        return None

    return SandboxContainerToolBinding(id=id, generation=generation)


async def resolve_shell_container(container_hash: str) -> SandboxContainer | None:
    async with get_async_session() as session:
        result = await session.exec(
            select(SandboxContainer).where(SandboxContainer.container_hash == container_hash)
        )
        sandbox_container = result.first()
        if sandbox_container is None or sandbox_container.status != SandboxContainerStatus.RUNNING:
            return None

    state = await asyncio.to_thread(_inspect_container_state_sync, container_hash)
    status = SandboxContainerStatus.ERROR if not state.exists else _docker_status_to_sandbox_status(state.status)
    if status != SandboxContainerStatus.RUNNING:
        if sandbox_container.id is not None:
            await _save_sandbox_container_status(sandbox_container.id, status)
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


def _close_shell_response_sync(socket: object, response: object | None) -> None:
    if response is None:
        response = getattr(socket, "_response", None)

    close = getattr(response, "close", None)
    if callable(close):
        try:
            close()
        except Exception:
            pass

    if response is not None:
        try:
            if getattr(socket, "_response", None) is response:
                setattr(socket, "_response", None)
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


async def execute_sandbox_container_command(id: int, command: str) -> SandboxContainerCommandResult:
    command = command.strip()
    if not command:
        raise ValueError("sandbox container command is required")

    async with get_async_session() as session:
        sandbox_container = await session.get(SandboxContainer, id)
        if sandbox_container is None:
            raise ValueError("sandbox container not found")
        if sandbox_container.status != SandboxContainerStatus.RUNNING:
            raise ValueError("only running sandbox containers can execute commands")

        container_hash = sandbox_container.container_hash

    try:
        state = await asyncio.to_thread(_inspect_container_state_sync, container_hash)
    except Exception:
        logger.exception("sandbox container inspect failed before command execution: %s", id)
        raise RuntimeError("failed to inspect sandbox container")

    status = SandboxContainerStatus.ERROR if not state.exists else _docker_status_to_sandbox_status(state.status)
    if status != SandboxContainerStatus.RUNNING:
        await _save_sandbox_container_status(id, status)
        raise RuntimeError("sandbox container is not running")

    try:
        return await _execute_container_command(container_hash, command)
    except asyncio.CancelledError:
        raise
    except docker.errors.NotFound:
        logger.info("sandbox container instance not found while executing command: %s", id)
        await _save_sandbox_container_status(id, SandboxContainerStatus.ERROR)
        raise RuntimeError("sandbox container instance not found")
    except Exception:
        logger.exception("sandbox container command execution failed: %s", id)
        raise RuntimeError("failed to execute sandbox container command")


# ── container file operations ─────────────────────────────────────────────────


def _exec_in_container_sync(container_hash: str, cmd: str) -> tuple[str, int]:
    client = docker.from_env()
    try:
        exit_code, output = client.containers.get(container_hash).exec_run(
            cmd=["/bin/sh", "-c", cmd],
            stdout=True,
            stderr=True,
            stdin=False,
            tty=False,
            demux=False,
        )
        text = output.decode(errors="replace") if isinstance(output, bytes) else str(output or "")
        code = exit_code if isinstance(exit_code, int) else 1
        return text, code
    finally:
        client.close()


def _normalize_container_path(path: str, *, default: str = "/") -> str:
    raw = (path or default).strip()
    if "\x00" in raw:
        raise ValueError("path contains invalid characters")
    if not raw.startswith("/"):
        raw = f"/{raw}"
    normalized = posixpath.normpath(raw)
    return "/" if normalized in {"", "."} else normalized


def _join_container_path(parent: str, name: str) -> str:
    base = _normalize_container_path(parent)
    return f"/{name}" if base == "/" else f"{base}/{name}"


def _safe_upload_name(filename: str) -> str:
    name = os.path.basename((filename or "").replace("\\", "/")).strip()
    if name in {"", ".", ".."} or "/" in name or "\x00" in name:
        raise ValueError("invalid upload filename")
    return name


def _safe_download_name(name: str, *, fallback: str = "download") -> str:
    cleaned = os.path.basename((name or fallback).replace("\\", "/")).strip()
    if cleaned in {"", ".", ".."} or "\x00" in cleaned:
        return fallback
    return cleaned


def _download_base_name(path: str, stat: dict[str, Any] | None = None) -> str:
    stat_name = stat.get("name") if isinstance(stat, dict) else None
    raw_name = str(stat_name or posixpath.basename(path.rstrip("/")) or "root")
    return _safe_download_name(raw_name, fallback="root")


def _is_directory_stat(stat: dict[str, Any] | None) -> bool:
    if not isinstance(stat, dict):
        return False
    try:
        mode = int(stat.get("mode", 0))
    except (TypeError, ValueError):
        return False
    return stat_module.S_ISDIR(mode)


def _copy_fileobj_count(source: BinaryIO, destination: BinaryIO) -> int:
    total = 0
    while True:
        chunk = source.read(_ARCHIVE_CHUNK_SIZE)
        if not chunk:
            return total
        destination.write(chunk)
        total += len(chunk)


def _iter_file_chunks(fileobj: BinaryIO) -> Iterator[bytes]:
    try:
        fileobj.seek(0)
        while True:
            chunk = fileobj.read(_ARCHIVE_CHUNK_SIZE)
            if not chunk:
                return
            yield chunk
    finally:
        fileobj.close()


def _iter_docker_chunks(client: docker.DockerClient, chunks: Iterator[bytes]) -> Iterator[bytes]:
    try:
        yield from chunks
    finally:
        client.close()


def _spool_chunks(chunks: Iterator[bytes], *, max_size: int) -> BinaryIO:
    archive = tempfile.SpooledTemporaryFile(max_size=max_size, mode="w+b")
    try:
        for chunk in chunks:
            archive.write(chunk)
        archive.seek(0)
        return archive
    except Exception:
        archive.close()
        raise


def _container_directory_exists(container_hash: str, path: str) -> bool:
    _, exit_code = _exec_in_container_sync(container_hash, f"test -d {shlex.quote(path)}")
    return exit_code == 0


def _container_path_exists(container_hash: str, path: str) -> bool:
    _, exit_code = _exec_in_container_sync(container_hash, f"test -e {shlex.quote(path)}")
    return exit_code == 0


def _upload_container_files_sync(
    container_hash: str,
    path: str,
    sources: list[ContainerUploadSource],
    overwrite: bool,
) -> list[ContainerFileUploadItem]:
    destination = _normalize_container_path(path)
    if not sources:
        raise ValueError("no files uploaded")
    if not _container_directory_exists(container_hash, destination):
        raise FileNotFoundError("destination directory not found")

    archive = tempfile.SpooledTemporaryFile(max_size=_UPLOAD_ARCHIVE_MEMORY_LIMIT, mode="w+b")
    uploaded: list[ContainerFileUploadItem] = []
    seen_names: set[str] = set()
    try:
        with tarfile.open(fileobj=archive, mode="w") as tar:
            for source in sources:
                name = _safe_upload_name(source.filename)
                if name in seen_names:
                    raise ValueError(f"duplicate upload filename: {name}")
                seen_names.add(name)

                target_path = _join_container_path(destination, name)
                if not overwrite and _container_path_exists(container_hash, target_path):
                    raise FileExistsError(f"file already exists: {target_path}")

                try:
                    source.stream.seek(0)
                except (AttributeError, OSError):
                    pass

                payload = tempfile.SpooledTemporaryFile(max_size=_UPLOAD_ARCHIVE_MEMORY_LIMIT, mode="w+b")
                try:
                    size = _copy_fileobj_count(source.stream, payload)
                    payload.seek(0)

                    info = tarfile.TarInfo(name=name)
                    info.size = size
                    info.mode = 0o644
                    info.mtime = int(time.time())
                    tar.addfile(info, payload)
                finally:
                    payload.close()

                uploaded.append(ContainerFileUploadItem(name=name, path=target_path, size=size))

        archive.seek(0)
        client = docker.from_env()
        try:
            ok = client.containers.get(container_hash).put_archive(destination, archive)
        finally:
            client.close()
        if not ok:
            raise RuntimeError("failed to upload files")
        return uploaded
    finally:
        archive.close()


def _extract_single_file_archive(path: str, stat: dict[str, Any] | None, archive: BinaryIO) -> ContainerDownloadStream:
    archive.seek(0)
    output = tempfile.SpooledTemporaryFile(max_size=_DOWNLOAD_ARCHIVE_MEMORY_LIMIT, mode="w+b")
    try:
        with tarfile.open(fileobj=archive, mode="r:*") as tar:
            for member in tar.getmembers():
                if not member.isfile():
                    continue
                source = tar.extractfile(member)
                if source is None:
                    continue
                with source:
                    shutil.copyfileobj(source, output, length=_ARCHIVE_CHUNK_SIZE)
                filename = _download_base_name(path, stat)
                output.seek(0)
                return ContainerDownloadStream(
                    filename=filename,
                    media_type="application/octet-stream",
                    chunks=_iter_file_chunks(output),
                )
        raise RuntimeError("download archive did not contain a file")
    except Exception:
        output.close()
        raise


def _download_single_path_sync(container_hash: str, path: str) -> ContainerDownloadStream:
    container_path = _normalize_container_path(path)
    client = docker.from_env()
    try:
        chunks, stat = client.containers.get(container_hash).get_archive(
            container_path,
            chunk_size=_ARCHIVE_CHUNK_SIZE,
        )
    except docker.errors.NotFound as exc:
        client.close()
        raise FileNotFoundError("path not found") from exc
    except Exception:
        client.close()
        raise

    if _is_directory_stat(stat):
        return ContainerDownloadStream(
            filename=f"{_download_base_name(container_path, stat)}.tar",
            media_type="application/x-tar",
            chunks=_iter_docker_chunks(client, chunks),
        )

    try:
        archive = _spool_chunks(chunks, max_size=_DOWNLOAD_ARCHIVE_MEMORY_LIMIT)
    finally:
        client.close()

    try:
        return _extract_single_file_archive(container_path, stat, archive)
    finally:
        archive.close()


def _safe_tar_member_name(name: str) -> str:
    cleaned = posixpath.normpath(str(name or "").replace("\\", "/")).lstrip("/")
    if cleaned in {"", ".", ".."} or cleaned.startswith("../"):
        raise ValueError("unsafe tar member name")
    return cleaned


def _unique_tar_root(name: str, used: set[str]) -> str:
    base = _safe_download_name(name, fallback="item")
    candidate = base
    index = 2
    while candidate in used:
        candidate = f"{base}-{index}"
        index += 1
    used.add(candidate)
    return candidate


def _remap_tar_member_name(member_name: str, source_root: str, target_root: str) -> str:
    safe_name = _safe_tar_member_name(member_name)
    if safe_name == source_root:
        return target_root
    prefix = f"{source_root}/"
    if safe_name.startswith(prefix):
        return f"{target_root}/{safe_name[len(prefix):]}"
    return f"{target_root}/{safe_name}"


def _copy_archive_into_tar(target_tar: tarfile.TarFile, archive: BinaryIO, target_root: str) -> None:
    archive.seek(0)
    with tarfile.open(fileobj=archive, mode="r:*") as source_tar:
        members = source_tar.getmembers()
        if not members:
            return
        source_root = _safe_tar_member_name(members[0].name).split("/", 1)[0]
        for member in members:
            copied = copy.copy(member)
            copied.name = _remap_tar_member_name(member.name, source_root, target_root)
            if member.isfile():
                source = source_tar.extractfile(member)
                if source is None:
                    continue
                with source:
                    target_tar.addfile(copied, source)
            else:
                target_tar.addfile(copied)


def _download_multiple_paths_sync(container_hash: str, paths: list[str]) -> ContainerDownloadStream:
    normalized_paths = [_normalize_container_path(path) for path in paths]
    if not normalized_paths:
        raise ValueError("download path is required")

    output = tempfile.SpooledTemporaryFile(max_size=_DOWNLOAD_ARCHIVE_MEMORY_LIMIT, mode="w+b")
    client = docker.from_env()
    try:
        container = client.containers.get(container_hash)
        used_roots: set[str] = set()
        with tarfile.open(fileobj=output, mode="w") as target_tar:
            for container_path in normalized_paths:
                try:
                    chunks, stat = container.get_archive(container_path, chunk_size=_ARCHIVE_CHUNK_SIZE)
                except docker.errors.NotFound as exc:
                    raise FileNotFoundError(f"path not found: {container_path}") from exc
                archive = _spool_chunks(chunks, max_size=_DOWNLOAD_ARCHIVE_MEMORY_LIMIT)
                try:
                    target_root = _unique_tar_root(_download_base_name(container_path, stat), used_roots)
                    _copy_archive_into_tar(target_tar, archive, target_root)
                finally:
                    archive.close()
    except Exception:
        output.close()
        raise
    finally:
        client.close()

    output.seek(0)
    return ContainerDownloadStream(
        filename="container-files.tar",
        media_type="application/x-tar",
        chunks=_iter_file_chunks(output),
    )


def _parse_find_output(raw: str, base_path: str) -> list[ContainerFileInfo]:
    files: list[ContainerFileInfo] = []
    for line in raw.strip().split("\n"):
        if not line:
            continue
        parts = line.split("\t")
        if len(parts) != 7:
            continue
        name, type_char, size_str, mtime_str, owner, group, perms = parts
        type_map = {"f": ContainerFileType.FILE, "d": ContainerFileType.DIRECTORY, "l": ContainerFileType.SYMLINK}
        file_type = type_map.get(type_char)
        if file_type is None:
            continue
        try:
            size = int(size_str)
        except ValueError:
            size = 0
        try:
            modified_at = int(float(mtime_str))
        except ValueError:
            modified_at = 0
        base = base_path.rstrip("/")
        files.append(ContainerFileInfo(
            name=name,
            type=file_type,
            size=size,
            modified_at=modified_at,
            owner=owner,
            group=group,
            permissions=perms,
            path=f"{base}/{name}",
        ))
    return files


async def list_container_files(container_hash: str, path: str) -> list[ContainerFileInfo]:
    safe_path = shlex.quote(path)
    cmd = f"find {safe_path} -maxdepth 1 -mindepth 1 -printf '%f\\t%y\\t%s\\t%T@\\t%u\\t%g\\t%#m\\n' 2>/dev/null"
    stdout, exit_code = await asyncio.to_thread(_exec_in_container_sync, container_hash, cmd)
    if exit_code != 0:
        raise RuntimeError(f"failed to list container files (exit code {exit_code}): {stdout.strip() or '(empty output)'}")
    return _parse_find_output(stdout, path)


async def get_container_file_info(container_hash: str, path: str) -> ContainerFileInfo | None:
    parent = path.rsplit("/", 1)[0] or "/"
    name = path.rsplit("/", 1)[-1] or path
    safe_parent = shlex.quote(parent)
    safe_name = shlex.quote(name)
    cmd = f"find {safe_parent} -maxdepth 1 -name {safe_name} -printf '%f\\t%y\\t%s\\t%T@\\t%u\\t%g\\t%#m\\n' 2>/dev/null"
    stdout, _exit_code = await asyncio.to_thread(_exec_in_container_sync, container_hash, cmd)
    files = _parse_find_output(stdout, parent)
    return files[0] if files else None


async def read_container_file(container_hash: str, path: str, max_bytes: int = 1_048_576, *, base64_mode: bool = False) -> str:
    safe_path = shlex.quote(path)
    if base64_mode:
        cmd = f"base64 {safe_path} 2>/dev/null | head -c {max_bytes * 2}"
    else:
        cmd = f"head -c {max_bytes} {safe_path} 2>/dev/null"
    stdout, exit_code = await asyncio.to_thread(_exec_in_container_sync, container_hash, cmd)
    if exit_code != 0:
        raise RuntimeError(f"failed to read container file: {stdout.strip()}")
    return stdout


async def upload_container_files(
    container_hash: str,
    path: str,
    sources: list[ContainerUploadSource],
    overwrite: bool,
) -> list[ContainerFileUploadItem]:
    return await asyncio.to_thread(_upload_container_files_sync, container_hash, path, sources, overwrite)


async def download_container_paths(container_hash: str, paths: list[str]) -> ContainerDownloadStream:
    if len(paths) == 1:
        return await asyncio.to_thread(_download_single_path_sync, container_hash, paths[0])
    return await asyncio.to_thread(_download_multiple_paths_sync, container_hash, paths)


async def write_container_file(container_hash: str, path: str, content: str) -> bool:
    encoded = base64.b64encode(content.encode()).decode()
    safe_path = shlex.quote(path)
    cmd = f"echo {shlex.quote(encoded)} | base64 -d > {safe_path}"
    stdout, exit_code = await asyncio.to_thread(_exec_in_container_sync, container_hash, cmd)
    return exit_code == 0


async def copy_container_files(container_hash: str, sources: list[str], destination: str) -> bool:
    quoted_sources = " ".join(shlex.quote(src) for src in sources)
    safe_dest = shlex.quote(destination)
    cmd = f"cp -r {quoted_sources} {safe_dest}"
    stdout, exit_code = await asyncio.to_thread(_exec_in_container_sync, container_hash, cmd)
    return exit_code == 0


async def move_container_files(container_hash: str, sources: list[str], destination: str) -> bool:
    quoted_sources = " ".join(shlex.quote(src) for src in sources)
    safe_dest = shlex.quote(destination)
    cmd = f"mv {quoted_sources} {safe_dest}"
    stdout, exit_code = await asyncio.to_thread(_exec_in_container_sync, container_hash, cmd)
    return exit_code == 0


async def delete_container_files(container_hash: str, paths: list[str]) -> bool:
    quoted_paths = " ".join(shlex.quote(p) for p in paths)
    cmd = f"rm -rf {quoted_paths}"
    stdout, exit_code = await asyncio.to_thread(_exec_in_container_sync, container_hash, cmd)
    return exit_code == 0


async def create_container_directory(container_hash: str, path: str) -> bool:
    safe_path = shlex.quote(path)
    cmd = f"mkdir -p {safe_path}"
    stdout, exit_code = await asyncio.to_thread(_exec_in_container_sync, container_hash, cmd)
    return exit_code == 0


async def resolve_file_container(id: int) -> tuple[str, SandboxContainerStatus] | None:
    async with get_async_session() as session:
        sandbox_container = await session.get(SandboxContainer, id)
        if sandbox_container is None:
            return None
        return sandbox_container.container_hash, sandbox_container.status
