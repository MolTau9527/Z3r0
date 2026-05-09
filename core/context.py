from dataclasses import dataclass
from schema.system_user_schema import SystemUserRole


@dataclass(frozen=True)
class AgentUserContext:
    id: int
    username: str
    email: str
    role: SystemUserRole


@dataclass
class AgentRuntimeContext:
    session_id: str
    user: AgentUserContext
    agent_code: str = ""
    knowledge_generation: int = 0
    sandbox_container_id: int | None = None
    sandbox_container_generation: int = 0
    sandbox_skill_metadata: tuple[str, ...] = ()
