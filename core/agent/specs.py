from dataclasses import dataclass

from agents import Tool

from core.agent.constants import DEFAULT_AGENT_CODE
from core.tools.knowledge import create_knowledge, find_knowledge, load_knowledge, update_knowledge
from core.tools.work_project import (
    load_work_project_agent_summaries,
    load_work_project_metadata,
    load_work_project_target_assets,
    load_work_project_tasks,
    update_work_project_agent_summary,
    update_work_project_tasks,
)
from core.tools.sandbox import (
    cancel_sandbox_async_job,
    execute_async_command,
    execute_sync_command,
    list_sandbox_async_jobs,
    load_skill,
    read_sandbox_command_output,
    wait_sandbox_async_job,
)


@dataclass(frozen=True, slots=True)
class ToolMount:
    tool: Tool
    requires_sandbox_container: bool = False
    requires_work_project: bool = False


@dataclass(frozen=True, slots=True)
class SubagentMount:
    code: str


@dataclass(frozen=True, slots=True)
class AgentSpec:
    code: str
    tools: tuple[ToolMount, ...] = ()
    subagents: tuple[SubagentMount, ...] = ()


KNOWLEDGE_TOOLS = (
    ToolMount(find_knowledge),
    ToolMount(load_knowledge),
    ToolMount(create_knowledge),
    ToolMount(update_knowledge),
)

WORK_PROJECT_TOOLS = (
    ToolMount(load_work_project_metadata, requires_work_project=True),
    ToolMount(load_work_project_target_assets, requires_work_project=True),
    ToolMount(load_work_project_tasks, requires_work_project=True),
    ToolMount(load_work_project_agent_summaries, requires_work_project=True),
    ToolMount(update_work_project_agent_summary, requires_work_project=True),
)

SANDBOX_TOOLS = (
    ToolMount(execute_sync_command, requires_sandbox_container=True),
    ToolMount(read_sandbox_command_output, requires_sandbox_container=True),
    ToolMount(execute_async_command, requires_sandbox_container=True),
    ToolMount(list_sandbox_async_jobs, requires_sandbox_container=True),
    ToolMount(wait_sandbox_async_job, requires_sandbox_container=True),
    ToolMount(cancel_sandbox_async_job, requires_sandbox_container=True),
    ToolMount(load_skill, requires_sandbox_container=True),
)


AGENT_SPECS: tuple[AgentSpec, ...] = (
    AgentSpec(
        code="cso",
        tools=(
            *KNOWLEDGE_TOOLS,
            *WORK_PROJECT_TOOLS,
            ToolMount(update_work_project_tasks, requires_work_project=True),
        ),
        subagents=(
            SubagentMount(code="cae"),
            SubagentMount(code="cce"),
            SubagentMount(code="cie"),
            SubagentMount(code="cpe"),
            SubagentMount(code="cre"),
        ),
    ),
    AgentSpec(
        code="cae",
        tools=(
            *SANDBOX_TOOLS,
            *KNOWLEDGE_TOOLS,
            *WORK_PROJECT_TOOLS,
        ),
    ),
    AgentSpec(
        code="cce",
        tools=(
            *SANDBOX_TOOLS,
            *KNOWLEDGE_TOOLS,
            *WORK_PROJECT_TOOLS,
        ),
    ),
    AgentSpec(
        code="cie",
        tools=(
            *SANDBOX_TOOLS,
            *KNOWLEDGE_TOOLS,
            *WORK_PROJECT_TOOLS,
        ),
    ),
    AgentSpec(
        code="cpe",
        tools=(
            *SANDBOX_TOOLS,
            *KNOWLEDGE_TOOLS,
            *WORK_PROJECT_TOOLS,
        ),
    ),
    AgentSpec(
        code="cre",
        tools=(
            *SANDBOX_TOOLS,
            *KNOWLEDGE_TOOLS,
            *WORK_PROJECT_TOOLS,
        ),
    ),
)
