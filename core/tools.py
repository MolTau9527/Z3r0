import asyncio

from agents import RunContextWrapper, function_tool

from core.context import AgentRuntimeContext
from schema.tool_result_schema import ToolResultSchema, ToolResultStatusSchema, ToolResultTypeSchema
from service.sandbox_container_service import execute_sandbox_container_command


@function_tool
async def execute_command(ctx: RunContextWrapper[AgentRuntimeContext], command: str) -> str:
    """Execute a command in the selected sandbox container.
    
    Args:
        command: The command to execute.

    Returns:
        The result of the command execution.
    """
    container_id = ctx.context.sandbox_container_id
    if container_id is None:
        return ToolResultSchema(
            status=ToolResultStatusSchema.ERROR,
            type=ToolResultTypeSchema.COMMAND_EXECUTION,
            output="No sandbox container selected.",
        ).model_dump_json()

    try:
        result = await execute_sandbox_container_command(id=container_id, command=command)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        return ToolResultSchema(
            status=ToolResultStatusSchema.ERROR,
            type=ToolResultTypeSchema.COMMAND_EXECUTION,
            output=str(exc) or "Command execution failed.",
        ).model_dump_json()

    return ToolResultSchema(
        status=ToolResultStatusSchema.SUCCESS if result.exit_code == 0 else ToolResultStatusSchema.ERROR,
        type=ToolResultTypeSchema.COMMAND_EXECUTION,
        output=result.output,
        exit_code=result.exit_code,
    ).model_dump_json()
