from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, Field


class AgentSubordinateStatus(StrEnum):
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELED = "canceled"


class AgentSubordinateTaskSnapshot(BaseModel):
    run_id: str
    session_id: str
    parent_agent_code: str
    parent_agent_instance_id: str = ""
    agent_code: str
    agent_name: str = ""
    status: AgentSubordinateStatus
    brief: str = ""
    result: str = ""
    error: str = ""
    progress: str = ""
    nested_call_id: str = ""
    created_at: datetime
    updated_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None


class AgentSubordinateTaskToolItem(BaseModel):
    run_id: str
    agent_code: str
    agent_name: str = ""
    status: AgentSubordinateStatus
    result: str = ""
    error: str = ""
    progress: str = ""


class AgentSubordinateTaskToolResult(BaseModel):
    task: AgentSubordinateTaskToolItem | None = None
    tasks: list[AgentSubordinateTaskToolItem] = Field(default_factory=list)
    message: str = ""
