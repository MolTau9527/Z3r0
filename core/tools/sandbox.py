import asyncio
import re
import shlex

from agents import RunContextWrapper, function_tool

from core.runtime.context import AgentRuntimeContext
from core.sandbox import command_output
from core.sandbox.command_jobs import cancel_async_sandbox_command, start_async_sandbox_command
from schema.sandbox.async_jobs import SandboxAsyncJobSnapshot, SandboxAsyncJobStatus
from schema.sandbox.command_outputs import SandboxAsyncJobListToolResult, SandboxAsyncJobToolResult, SandboxCommandResultMetadata
from schema.common.tool_results import ToolResultSchema, ToolResultStatusSchema, ToolResultTypeSchema
from service.sandbox import async_jobs as sandbox_async_jobs
from service.sandbox.commands import SandboxContainerCommandTimeoutError, execute_sandbox_container_command
from utils.markdown import markdown_body_without_front_matter


_SKILL_NAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
SANDBOX_SKILLS_DIR = "/root/.agents/skills"
_SYNC_COMMAND_TIMEOUT_SECONDS = 30
_ASYNC_COMMAND_TIMEOUT_SECONDS = 300
_ASYNC_JOB_WAIT_TIMEOUT_SECONDS = 60
_COMMAND_TIMEOUT_ERROR = "Command execution timed out."


def _command_tool_result(
    *,
    status: SandboxAsyncJobStatus,
    output_file: str | None = None,
    output_bytes: int = 0,
    output_lines: int = 0,
    exit_code: int | None = None,
    run_id: str | None = None,
    error: str | None = None,
) -> str:
    return command_output.result_metadata(
        status=status,
        output_file=output_file,
        output_bytes=output_bytes,
        output_lines=output_lines,
        exit_code=exit_code,
        run_id=run_id,
        error=error,
    ).model_dump_json(exclude_none=True)


def _command_required_error() -> str:
    return _command_tool_result(
        status=SandboxAsyncJobStatus.FAILED,
        error="sandbox container command is required",
    )


def _no_container_error() -> str:
    return _command_tool_result(
        status=SandboxAsyncJobStatus.FAILED,
        error="No sandbox container selected.",
    )


def _clamp_timeout_seconds(timeout_seconds: int | None, maximum: int) -> int:
    if timeout_seconds is None:
        return maximum
    try:
        timeout_seconds = int(timeout_seconds)
    except (TypeError, ValueError):
        return maximum
    return min(max(timeout_seconds, 1), maximum)


def _clamp_wait_seconds(wait_seconds: int | None) -> int:
    if wait_seconds is None:
        return 10
    try:
        wait_seconds = int(wait_seconds)
    except (TypeError, ValueError):
        return 10
    return min(max(wait_seconds, 0), _ASYNC_JOB_WAIT_TIMEOUT_SECONDS)


def _async_job_tool_output(snapshot: SandboxAsyncJobSnapshot) -> SandboxCommandResultMetadata:
    return command_output.result_metadata(
        status=snapshot.status,
        output_file=snapshot.output_file,
        output_bytes=snapshot.output_bytes,
        output_lines=snapshot.output_lines,
        exit_code=snapshot.exit_code,
        run_id=snapshot.run_id,
        error=snapshot.error,
    )


def _async_job_list_item(snapshot: SandboxAsyncJobSnapshot) -> SandboxAsyncJobToolResult:
    return SandboxAsyncJobToolResult(
        run_id=snapshot.run_id,
        status=snapshot.status,
        output_file=snapshot.output_file,
        output_bytes=snapshot.output_bytes,
        output_lines=snapshot.output_lines,
        exit_code=snapshot.exit_code,
        error=snapshot.error or None,
    )


@function_tool
async def execute_sync_command(
    ctx: RunContextWrapper[AgentRuntimeContext],
    command: str,
    timeout_seconds: int = _SYNC_COMMAND_TIMEOUT_SECONDS,
) -> str:
    """Execute a short sandbox command and return result metadata.
    
    Args:
        command: str shell command to execute in the selected sandbox container.
        timeout_seconds: int command timeout in seconds, clamped to 1-30.

    Returns:
        JSON metadata with status, output_file, output_bytes, output_lines, exit_code, and optional error.
    """
    container_id = ctx.context.sandbox_container_id
    if container_id is None:
        return _no_container_error()
    if not command.strip():
        return _command_required_error()
    normalized_timeout_seconds = _clamp_timeout_seconds(timeout_seconds, _SYNC_COMMAND_TIMEOUT_SECONDS)
    output_path = command_output.new_output_path()

    try:
        result = await execute_sandbox_container_command(
            id=container_id,
            command=command_output.capture_command(command, output_path),
            timeout_seconds=normalized_timeout_seconds,
        )
    except asyncio.CancelledError:
        raise
    except SandboxContainerCommandTimeoutError:
        return _command_tool_result(
            status=SandboxAsyncJobStatus.FAILED,
            error=_COMMAND_TIMEOUT_ERROR,
        )
    except Exception as exc:
        return _command_tool_result(
            status=SandboxAsyncJobStatus.FAILED,
            error=str(exc) or "Command execution failed.",
        )

    output_bytes, output_lines = command_output.parse_capture_stats(result.output)
    success = result.exit_code == 0
    return _command_tool_result(
        status=SandboxAsyncJobStatus.COMPLETED if success else SandboxAsyncJobStatus.FAILED,
        output_file=output_path,
        output_bytes=output_bytes,
        output_lines=output_lines,
        exit_code=result.exit_code,
    )


@function_tool
async def read_sandbox_command_output(
    ctx: RunContextWrapper[AgentRuntimeContext],
    output_file: str,
    start_line: int = 1,
    line_count: int = command_output.OUTPUT_CHUNK_LINE_COUNT,
) -> str:
    """Read a bounded line range from a sandbox command output file.

    Args:
        output_file: str output path returned by execute_sync_command or execute_async_command.
        start_line: int one-based starting line number.
        line_count: int number of lines to read, clamped by the output reader to a bounded chunk size.

    Returns:
        JSON chunk metadata with output_file, start_line, end_line, content, and line boundary flags.
    """
    container_id = ctx.context.sandbox_container_id
    if container_id is None:
        return _no_container_error()
    try:
        command, start, count, end = command_output.read_command(output_file, start_line, line_count)
        result = await execute_sandbox_container_command(
            id=container_id,
            command=command,
            timeout_seconds=_SYNC_COMMAND_TIMEOUT_SECONDS,
        )
    except asyncio.CancelledError:
        raise
    except ValueError as exc:
        return _command_tool_result(
            status=SandboxAsyncJobStatus.FAILED,
            error=str(exc),
        )
    except SandboxContainerCommandTimeoutError:
        return _command_tool_result(
            status=SandboxAsyncJobStatus.FAILED,
            error=_COMMAND_TIMEOUT_ERROR,
        )
    except Exception as exc:
        return _command_tool_result(
            status=SandboxAsyncJobStatus.FAILED,
            error=str(exc) or "Command output read failed.",
        )
    if result.exit_code != 0:
        return _command_tool_result(
            status=SandboxAsyncJobStatus.FAILED,
            error=result.output or "Command output read failed.",
        )

    return command_output.output_chunk(
        output_file=output_file,
        start_line=start,
        line_count=count,
        content=result.output,
    ).model_dump_json(exclude_none=True)


@function_tool
async def execute_async_command(
    ctx: RunContextWrapper[AgentRuntimeContext],
    command: str,
    timeout_seconds: int = _ASYNC_COMMAND_TIMEOUT_SECONDS,
) -> str:
    """Start a long-running sandbox command and return running metadata.
    
    Args:
        command: str shell command to execute in the selected sandbox container.
        timeout_seconds: int command timeout in seconds, clamped to 1-300.

    Returns:
        JSON metadata with status, run_id, output_file, and empty output stats. Continue independent work, use wait_sandbox_async_job once for a bounded dependency wait, or end the turn for automatic completion notification.
    """
    container_id = ctx.context.sandbox_container_id
    if container_id is None:
        return _no_container_error()
    if not command.strip():
        return _command_required_error()
    if not ctx.context.agent_instance_id:
        return _command_tool_result(
            status=SandboxAsyncJobStatus.FAILED,
            error="agent instance id is required for async command execution",
        )
    normalized_timeout_seconds = _clamp_timeout_seconds(timeout_seconds, _ASYNC_COMMAND_TIMEOUT_SECONDS)
    running_jobs = await sandbox_async_jobs.count_running_async_jobs_for_agent(
        session_id=ctx.context.session_id,
        agent_instance_id=ctx.context.agent_instance_id,
    )
    if running_jobs >= 3:
        return _command_tool_result(
            status=SandboxAsyncJobStatus.FAILED,
            error="sandbox async command limit reached; at most 3 commands may run concurrently",
        )

    run_id = command_output.new_run_id()
    output_path = command_output.output_path_for_run(run_id)
    command_text = command.strip()
    task_context = AgentRuntimeContext(
        session_id=ctx.context.session_id,
        user=ctx.context.user,
        agent_code=ctx.context.agent_code,
        agent_instance_id=ctx.context.agent_instance_id,
        nested_for_agent_code=ctx.context.nested_for_agent_code,
        nested_call_id=ctx.context.nested_call_id,
        knowledge_generation=ctx.context.knowledge_generation,
        sandbox_container_id=ctx.context.sandbox_container_id,
        sandbox_container_generation=ctx.context.sandbox_container_generation,
        sandbox_skill_metadata=ctx.context.sandbox_skill_metadata,
        work_project_id=ctx.context.work_project_id,
    )
    await start_async_sandbox_command(
        run_id=run_id,
        context=task_context,
        command=command_text,
        output_file=output_path,
        wrapped_command=command_output.async_command(command_text, output_path),
        stat_command=command_output.stat_command(output_path),
        timeout_seconds=normalized_timeout_seconds,
    )
    return _command_tool_result(
        status=SandboxAsyncJobStatus.RUNNING,
        output_file=output_path,
        output_bytes=0,
        output_lines=0,
        exit_code=None,
        run_id=run_id,
    )


@function_tool
async def list_sandbox_async_jobs(
    ctx: RunContextWrapper[AgentRuntimeContext],
    running_only: bool = False,
    limit: int = 20,
) -> str:
    """List sandbox async commands owned by the current agent instance.

    This is an inspection/capacity tool, not a wait primitive. Do not call it in loops to wait for completion.

    Args:
        running_only: bool whether to include only commands that are still running.
        limit: int maximum number of recent jobs to return.

    Returns:
        JSON object containing recent async command jobs and their status/output metadata.
    """
    jobs = await sandbox_async_jobs.list_async_jobs_for_agent(
        session_id=ctx.context.session_id,
        agent_instance_id=ctx.context.agent_instance_id,
        running_only=running_only,
        limit=limit,
    )
    return SandboxAsyncJobListToolResult(jobs=[
        _async_job_list_item(job) for job in jobs
    ]).model_dump_json(
        exclude_none=True,
        exclude_defaults=True,
    )


@function_tool
async def wait_sandbox_async_job(
    ctx: RunContextWrapper[AgentRuntimeContext],
    run_id: str,
    wait_seconds: int = 10,
) -> str:
    """Wait once for one sandbox async command and return its latest status.

    Args:
        run_id: str async command run id returned by execute_async_command or list_sandbox_async_jobs.
        wait_seconds: int seconds to wait, clamped to 0-60. Use 0 for immediate status.

    Returns:
        JSON metadata after the job reaches a terminal state or the wait expires. If status is still running, continue independent work or end the turn instead of waiting again.
    """
    normalized_run_id = run_id.strip()
    if not normalized_run_id:
        return _command_tool_result(
            status=SandboxAsyncJobStatus.FAILED,
            error="sandbox async job run_id is required",
        )
    wait = _clamp_wait_seconds(wait_seconds)
    snapshot = await sandbox_async_jobs.get_async_job(normalized_run_id, session_id=ctx.context.session_id)
    if snapshot is None or snapshot.agent_instance_id != ctx.context.agent_instance_id:
        return _command_tool_result(
            status=SandboxAsyncJobStatus.FAILED,
            error="sandbox async job not found",
        )
    latest = await sandbox_async_jobs.wait_async_job(
        snapshot.run_id,
        session_id=ctx.context.session_id,
        timeout_seconds=wait,
    )
    result = latest or snapshot
    if result.status in sandbox_async_jobs.TERMINAL_ASYNC_JOB_STATUSES:
        result = await sandbox_async_jobs.mark_async_job_result_delivered(
            result.run_id,
            session_id=ctx.context.session_id,
        ) or result
    return _async_job_tool_output(result).model_dump_json(exclude_none=True)


@function_tool
async def cancel_sandbox_async_job(ctx: RunContextWrapper[AgentRuntimeContext], run_id: str) -> str:
    """Cancel a sandbox async command owned by the current session.

    Args:
        run_id: str async command run id returned by execute_async_command or list_sandbox_async_jobs.

    Returns:
        JSON metadata for the latest known async command state after cancellation is requested.
    """
    snapshot = await sandbox_async_jobs.get_async_job(run_id.strip(), session_id=ctx.context.session_id)
    if snapshot is None or snapshot.agent_instance_id != ctx.context.agent_instance_id:
        return _command_tool_result(
            status=SandboxAsyncJobStatus.FAILED,
            error="sandbox async job not found",
        )
    await cancel_async_sandbox_command(snapshot.run_id)
    latest = await sandbox_async_jobs.get_async_job(snapshot.run_id, session_id=ctx.context.session_id)
    return _async_job_tool_output(latest or snapshot).model_dump_json(exclude_none=True)


@function_tool
async def load_skill(ctx: RunContextWrapper[AgentRuntimeContext], name: str) -> str:
    """Load the body of a named skill from the selected sandbox container.

    Args:
        name: str skill directory name under /root/.agents/skills.

    Returns:
        JSON status with the skill detail markdown body without YAML Front Matter.
    """
    container_id = ctx.context.sandbox_container_id
    if container_id is None:
        return ToolResultSchema(
            status=ToolResultStatusSchema.ERROR,
            type=ToolResultTypeSchema.SKILL_DETAIL,
            output="No sandbox container selected.",
        ).model_dump_json()

    skill_name = name.strip()
    if not _SKILL_NAME_PATTERN.fullmatch(skill_name):
        return ToolResultSchema(
            status=ToolResultStatusSchema.ERROR,
            type=ToolResultTypeSchema.SKILL_DETAIL,
            output="Skill name must contain only letters, numbers, dot, underscore, or dash.",
        ).model_dump_json()

    skill_path = f"{SANDBOX_SKILLS_DIR}/{skill_name}/SKILL.md"
    command = f"test -f {shlex.quote(skill_path)} && cat {shlex.quote(skill_path)}"
    try:
        result = await execute_sandbox_container_command(
            id=container_id,
            command=command,
            timeout_seconds=_SYNC_COMMAND_TIMEOUT_SECONDS,
        )
    except asyncio.CancelledError:
        raise
    except SandboxContainerCommandTimeoutError:
        return ToolResultSchema(
            status=ToolResultStatusSchema.ERROR,
            type=ToolResultTypeSchema.SKILL_DETAIL,
            output=_COMMAND_TIMEOUT_ERROR,
        ).model_dump_json()
    except Exception as exc:
        return ToolResultSchema(
            status=ToolResultStatusSchema.ERROR,
            type=ToolResultTypeSchema.SKILL_DETAIL,
            output=str(exc) or "Skill loading failed.",
        ).model_dump_json()

    if result.exit_code != 0:
        return ToolResultSchema(
            status=ToolResultStatusSchema.ERROR,
            type=ToolResultTypeSchema.SKILL_DETAIL,
            output=f"Skill not found: {skill_name}",
        ).model_dump_json()

    return ToolResultSchema(
        status=ToolResultStatusSchema.SUCCESS,
        type=ToolResultTypeSchema.SKILL_DETAIL,
        output=markdown_body_without_front_matter(result.output),
    ).model_dump_json()
