"""Agent declarations and session-bound SDK Agent construction."""

from __future__ import annotations

from dataclasses import dataclass

from agents import Agent, Tool
from agents.extensions.models.litellm_model import LitellmModel

from config import AgentConfig, WORKSPACE, get_config
from core import subordinates
from core.context import AgentRuntimeContext
from core.tools.knowledge_tool import create_knowledge, load_knowledge, load_knowledge_metadata, update_knowledge
from core.tools.sandbox_tool import load_skill, execute_command
from logger import get_logger


logger = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class ToolMount:
    tool: Tool
    requires_sandbox_container: bool = False


@dataclass(frozen=True, slots=True)
class SubagentMount:
    code: str


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


DEFAULT_AGENT_CODE = "cso"
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
                code="csa",
            ),
            SubagentMount(
                code="cse",
            ),
        ),
    ),
    AgentSpec(
        code="csa",
        tools=(
            ToolMount(execute_command, requires_sandbox_container=True),
            ToolMount(load_skill, requires_sandbox_container=True),
            *KNOWLEDGE_TOOLS,
        ),
    ),
    AgentSpec(
        code="cse",
        tools=(
            ToolMount(execute_command, requires_sandbox_container=True),
            ToolMount(load_skill, requires_sandbox_container=True),
            *KNOWLEDGE_TOOLS,
        ),
    ),
)


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
                subordinates.build_subagent_tools(
                    spec.code,
                    (mount.code for mount in spec.subagents),
                    get_child_agent=lambda code: graph.child(code).get(code),
                    get_code_to_name=graph.code_to_name,
                )
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
    header = (
        "# Agent Knowledge System\n\n"
        "This agent can maintain reusable professional knowledge under "
        f"`.z3r0/agents/{agent_code}/knowledges` with `create_knowledge`, `load_knowledge`, "
        "and `update_knowledge`. Use these tools only for durable, verified, role-specific professional "
        "knowledge. Do not store user preferences, user profiles, personal data, credentials, secrets, "
        "one-off task state, conversation summaries, speculative claims, or knowledge outside this agent's role. "
        "Before creating new knowledge, check whether an existing knowledge should be updated. Before updating "
        "body content, call `load_knowledge`; its line numbers are body-only and are the line numbers accepted "
        "by `update_knowledge`. The `update_knowledge` tool changes name and description through explicit "
        "parameters only; body line patches cannot edit YAML Front Matter.\n\n"
    )
    if not knowledge_metadata:
        return (
            header +
            f"No agent knowledges were discovered under `.z3r0/agents/{agent_code}/knowledges`."
        )

    return (
        header +
        "This agent exposes these knowledge metadata blocks from "
        f"`.z3r0/agents/{agent_code}/knowledges`. Only YAML Front Matter is shown here; use the "
        "`load_knowledge` tool with the knowledge file name without the `.md` suffix and optional body "
        "line range to read line-numbered knowledge body before applying or updating a knowledge.\n\n"
        + "\n\n".join(knowledge_metadata)
    )


def _build_sandbox_skill_instructions(skill_metadata: tuple[str, ...]) -> str:
    if not skill_metadata:
        return (
            "# Sandbox Skills\n\n"
            "No sandbox skills were discovered under `/root/.agents/skills` for the selected container."
        )

    return (
        "# Sandbox Skills\n\n"
        "The selected sandbox container exposes these skill metadata blocks from "
        "`/root/.agents/skills`. Only YAML Front Matter is shown here; use the "
        "`load_skill` tool to read the skill body before applying a skill.\n\n"
        + "\n\n".join(skill_metadata)
    )
