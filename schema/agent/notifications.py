from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field


class AgentNotificationKind(StrEnum):
    SUBAGENT_FINISHED = "subagent_finished"
    SANDBOX_ASYNC_JOB_FINISHED = "sandbox_async_job_finished"


class AgentNotificationStatus(StrEnum):
    PENDING = "pending"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELED = "canceled"


class AgentNotificationSnapshot(BaseModel):
    id: str
    session_id: str
    target_agent_code: str
    target_agent_instance_id: str
    nested_for_agent_code: str = ""
    nested_call_id: str = ""
    kind: AgentNotificationKind
    status: AgentNotificationStatus
    run_id: str = ""
    payload: dict[str, Any] = Field(default_factory=dict)
    error: str = ""
    sandbox_container_id: int | None = None
    sandbox_container_generation: int = 0
    sandbox_skill_metadata: tuple[str, ...] = ()
    created_at: datetime
    updated_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None
