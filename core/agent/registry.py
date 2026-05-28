"""Agent declarations and session-bound SDK Agent construction."""

from __future__ import annotations

from dataclasses import dataclass

from agents import Agent, Model, ModelSettings, Tool

from config import AgentConfig, WORKSPACE, get_config
from core.agent.instructions import build_instructions
from core.agent.models import build_openai_model
from core.agent.specs import AGENT_SPECS, AgentSpec, ToolMount
from core.delegation.subagents import build_subagent_tools
from core.runtime.context import AgentRuntimeContext
from core.tools.knowledge import find_knowledge, load_knowledge
from core.tools.sandbox import (
    execute_async_command,
    execute_sync_command,
    load_skill,
)
from logger import get_logger


logger = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class AgentToolSnapshot:
    knowledge_generation: int = 0
    sandbox_container_id: int | None = None
    sandbox_container_generation: int = 0
    sandbox_skill_metadata: tuple[str, ...] = ()
    work_project_id: int | None = None

    @classmethod
    def from_context(cls, context: AgentRuntimeContext) -> "AgentToolSnapshot":
        return cls(
            knowledge_generation=context.knowledge_generation,
            sandbox_container_id=context.sandbox_container_id,
            sandbox_container_generation=context.sandbox_container_generation,
            sandbox_skill_metadata=context.sandbox_skill_metadata,
            work_project_id=context.work_project_id,
        )


class AgentRegistry:
    def __init__(self, specs: tuple[AgentSpec, ...] = AGENT_SPECS) -> None:
        self._specs: dict[str, AgentSpec] = {spec.code: spec for spec in specs}
        self._codes_cache: tuple[str, ...] | None = None
        self._code_to_name_cache: dict[str, str] | None = None

    def codes(self) -> list[str]:
        if self._codes_cache is None:
            configured = set(get_config().agents.keys())
            self._codes_cache = tuple(code for code in self._specs if code in configured)
        return list(self._codes_cache)

    def code_to_name(self) -> dict[str, str]:
        if self._code_to_name_cache is None:
            cfg = get_config()
            self._code_to_name_cache = {code: cfg.agents[code].name for code in self.codes()}
        return self._code_to_name_cache

    def has(self, agent_code: str) -> bool:
        return agent_code in self.codes()

    def bind(self, tool_snapshot: AgentToolSnapshot) -> SessionAgentGraph:
        return SessionAgentGraph(self, tool_snapshot)

    def _spec(self, agent_code: str) -> AgentSpec:
        spec = self._specs.get(agent_code)
        if spec is None:
            raise ValueError(f"agent spec not declared for code: {agent_code}")
        return spec

    def _build(self, spec: AgentSpec, cfg: AgentConfig, graph: SessionAgentGraph) -> Agent:
        agent_path = WORKSPACE / "agents" / spec.code
        soul = (agent_path / "SOUL.md").read_text(encoding="utf-8").strip()
        rules = (agent_path / "AGENTS.md").read_text(encoding="utf-8").strip()
        instructions = build_instructions(
            soul,
            rules,
            spec.code,
            graph.tool_snapshot.sandbox_skill_metadata,
            has_sandbox_container=graph.tool_snapshot.sandbox_container_id is not None,
            include_sandbox_commands=_has_tool(spec, execute_sync_command) or _has_tool(spec, execute_async_command),
            include_sandbox_skills=_has_tool(spec, load_skill),
            include_agent_knowledges=_has_any_tool(spec, (find_knowledge, load_knowledge)),
            include_work_project_tools=(
                graph.tool_snapshot.work_project_id is not None
                and _has_work_project_tool(spec)
            ),
        )

        tools: list[Tool] = [
            mount.tool for mount in spec.tools
            if _tool_mount_available(mount, graph.tool_snapshot)
        ]
        for mount in spec.subagents:
            if mount.code == spec.code:
                raise ValueError(f"agent {spec.code} cannot mount itself as a subagent")
            child_graph = graph.child(mount.code)
            child_graph.get(mount.code)  # eager build catches circular mounts at boot
        if spec.subagents:
            tools.extend(
                _build_subagent_tools(spec, graph)
            )

        return Agent(
            name=cfg.name,
            model=build_openai_model(cfg),
            model_settings=ModelSettings(parallel_tool_calls=False),
            instructions=instructions,
            tools=tools,
        )


class SessionAgentGraph:
    def __init__(self, registry: AgentRegistry, tool_snapshot: AgentToolSnapshot) -> None:
        self._registry = registry
        self.tool_snapshot = tool_snapshot
        self._agents: dict[str, Agent] = {}
        self._models: list[Model] = []
        self._building: set[str] = set()
        self._children: dict[str, SessionAgentGraph] = {}

    def code_to_name(self) -> dict[str, str]:
        return self._registry.code_to_name()

    def get(self, agent_code: str) -> Agent:
        cached = self._agents.get(agent_code)
        if cached is not None:
            return cached
        if agent_code in self._building:
            raise ValueError(f"circular subagent mount detected at {agent_code}")

        spec = self._registry._spec(agent_code)
        cfg = get_config().agents.get(agent_code)
        if cfg is None:
            raise ValueError(f"agent config missing for code: {agent_code}")

        self._building.add(agent_code)
        try:
            agent = self._registry._build(spec, cfg, self)
            self._agents[agent_code] = agent
            self._models.append(agent.model)
            return agent
        finally:
            self._building.discard(agent_code)

    def child(self, mount_code: str) -> "SessionAgentGraph":
        child = self._children.get(mount_code)
        if child is None:
            child = SessionAgentGraph(self._registry, self.tool_snapshot)
            child._building.update(self._building)
            self._children[mount_code] = child
        return child

    async def close(self) -> None:
        for child in self._children.values():
            await child.close()
        for model in self._models:
            await model.close()
        self._children.clear()
        self._agents.clear()
        self._models.clear()
        self._building.clear()


def _has_tool(spec: AgentSpec, tool: Tool) -> bool:
    return any(mount.tool is tool for mount in spec.tools)


def _has_any_tool(spec: AgentSpec, tools: tuple[Tool, ...]) -> bool:
    return any(_has_tool(spec, tool) for tool in tools)


def _has_work_project_tool(spec: AgentSpec) -> bool:
    return any(mount.requires_work_project for mount in spec.tools)


def _tool_mount_available(mount: ToolMount, snapshot: AgentToolSnapshot) -> bool:
    if mount.requires_sandbox_container and snapshot.sandbox_container_id is None:
        return False
    if mount.requires_work_project and snapshot.work_project_id is None:
        return False
    return True


def _build_subagent_tools(spec: AgentSpec, graph: SessionAgentGraph) -> list[Tool]:
    return build_subagent_tools(
        spec.code,
        (mount.code for mount in spec.subagents),
        get_child_agent=lambda code: graph.child(code).get(code),
        get_code_to_name=graph.code_to_name,
    )
