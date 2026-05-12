"""Agent declarations and session-bound SDK Agent construction."""

from __future__ import annotations

from dataclasses import dataclass

from agents import Agent, Tool
from agents.extensions.models.litellm_model import LitellmModel

from config import AgentConfig, WORKSPACE, get_config
from core import subordinates
from core.context import AgentRuntimeContext
from core.tools.knowledge_tool import create_knowledge, load_knowledge, load_knowledge_metadata, update_knowledge
from core.tools.sandbox_tool import execute_async_command, execute_sync_command, load_skill
from logger import get_logger


logger = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class ToolMount:
    tool: Tool
    requires_sandbox_container: bool = False


@dataclass(frozen=True, slots=True)
class SubagentMount:
    code: str
    allow_wait: bool = True


@dataclass(frozen=True, slots=True)
class AgentSpec:
    code: str
    tools: tuple[ToolMount, ...] = ()
    subagents: tuple[SubagentMount, ...] = ()


@dataclass(frozen=True, slots=True)
class AgentToolSnapshot:
    knowledge_generation: int = 0
    sandbox_container_id: int | None = None
    sandbox_container_generation: int = 0
    sandbox_skill_metadata: tuple[str, ...] = ()

    @classmethod
    def from_context(cls, context: AgentRuntimeContext) -> "AgentToolSnapshot":
        return cls(
            knowledge_generation=context.knowledge_generation,
            sandbox_container_id=context.sandbox_container_id,
            sandbox_container_generation=context.sandbox_container_generation,
            sandbox_skill_metadata=context.sandbox_skill_metadata,
        )


KNOWLEDGE_TOOLS = (
    ToolMount(load_knowledge),
    ToolMount(create_knowledge),
    ToolMount(update_knowledge),
)

_AGENT_SPECS: tuple[AgentSpec, ...] = (
    AgentSpec(
        code="cso",
        tools=KNOWLEDGE_TOOLS,
        subagents=(
            SubagentMount(
                code="cie",
                allow_wait=False,
            ),
            SubagentMount(
                code="cpe",
                allow_wait=False,
            ),
        ),
    ),
    AgentSpec(
        code="cie",
        tools=(
            ToolMount(execute_sync_command, requires_sandbox_container=True),
            ToolMount(execute_async_command, requires_sandbox_container=True),
            ToolMount(load_skill, requires_sandbox_container=True),
            *KNOWLEDGE_TOOLS,
        ),
    ),
    AgentSpec(
        code="cpe",
        tools=(
            ToolMount(execute_sync_command, requires_sandbox_container=True),
            ToolMount(execute_async_command, requires_sandbox_container=True),
            ToolMount(load_skill, requires_sandbox_container=True),
            *KNOWLEDGE_TOOLS,
        ),
    ),
)


DEFAULT_AGENT_CODE = _AGENT_SPECS[0].code


class AgentRegistry:
    def __init__(self, specs: tuple[AgentSpec, ...] = _AGENT_SPECS) -> None:
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
        instructions = _build_instructions(
            soul,
            rules,
            spec.code,
            graph.tool_snapshot,
            include_sandbox_skills=_has_tool(spec, load_skill),
            include_agent_knowledges=_has_tool(spec, load_knowledge),
        )

        tools: list[Tool] = [
            mount.tool for mount in spec.tools
            if not mount.requires_sandbox_container or graph.tool_snapshot.sandbox_container_id is not None
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
            model=LitellmModel(base_url=cfg.base_url, api_key=cfg.api_key, model=cfg.model),
            instructions=instructions,
            tools=tools,
        )


class SessionAgentGraph:
    def __init__(self, registry: AgentRegistry, tool_snapshot: AgentToolSnapshot) -> None:
        self._registry = registry
        self.tool_snapshot = tool_snapshot
        self._agents: dict[str, Agent] = {}
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

    def close(self) -> None:
        for child in self._children.values():
            child.close()
        self._children.clear()
        self._agents.clear()
        self._building.clear()


def _has_tool(spec: AgentSpec, tool: Tool) -> bool:
    return any(mount.tool is tool for mount in spec.tools)


def _build_subagent_tools(spec: AgentSpec, graph: SessionAgentGraph) -> list[Tool]:
    return subordinates.build_subagent_tools(
        spec.code,
        (mount.code for mount in spec.subagents),
        get_child_agent=lambda code: graph.child(code).get(code),
        get_code_to_name=graph.code_to_name,
        allow_wait=any(mount.allow_wait for mount in spec.subagents),
    )


def _build_instructions(
    soul: str,
    rules: str,
    agent_code: str,
    tool_snapshot: AgentToolSnapshot,
    *,
    include_sandbox_skills: bool,
    include_agent_knowledges: bool,
) -> str:
    parts = [soul, rules]
    if include_agent_knowledges:
        parts.append(_build_agent_knowledge_instructions(agent_code, load_knowledge_metadata(agent_code)))
    if include_sandbox_skills and tool_snapshot.sandbox_container_id is not None:
        parts.append(_build_sandbox_skill_instructions(tool_snapshot.sandbox_skill_metadata))
    return "\n\n".join(part for part in parts if part)


def _build_agent_knowledge_instructions(agent_code: str, knowledge_metadata: tuple[str, ...]) -> str:
    if not knowledge_metadata:
        return (
            "\n\n# Knowledges\n\n"
            "No knowledge metadata."
        )

    return (
        "\n\n# Knowledges\n\n"
        "Available metadata only; read body before use or edit.\n\n"
        + "\n\n".join(knowledge_metadata)
    )


def _build_sandbox_skill_instructions(skill_metadata: tuple[str, ...]) -> str:
    if not skill_metadata:
        return (
            "\n\n# Sandbox Skills\n\n"
            "No sandbox skill metadata is available."
        )

    return (
        "\n\n# Sandbox Skills\n\n"
        "Available skill metadata. Only metadata is shown; read the skill body before applying.\n\n"
        + "\n\n".join(skill_metadata)
    )
