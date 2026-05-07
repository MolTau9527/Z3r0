import json
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field


ROOT_PATH = Path(__file__).resolve().parent
WORKSPACE = ROOT_PATH / ".z3r0"


# strict type config base model
class StrictConfigModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


# system config
class SystemConfig(StrictConfigModel):
    listen_addr: str = Field(default="127.0.0.1")
    listen_port: int = Field(default=8000)
    encrypt_key: str = Field(default="z3r0-jwt-secret-key-1234567890!@#$")


# database config
class DatabaseConfig(StrictConfigModel):
    host: str = Field(default="127.0.0.1")
    port: int = Field(default=5433)
    database: str = Field(default="z3r0")
    username: str = Field(default="")
    password: str = Field(default="")


# agent config
class AgentConfig(StrictConfigModel):
    code: str = Field(default="")
    name: str = Field(default="")
    description: str = Field(default="")
    base_url: str = Field(default="")
    api_key: str = Field(default="")
    model: str = Field(default="")


# per-process agent runtime pool tuning
class AgentPoolConfig(StrictConfigModel):
    max_size: int = Field(default=256, ge=1)
    ttl_seconds: int = Field(default=30 * 60, ge=0)
    sweep_interval_seconds: int = Field(default=60, ge=1)


# per-process agent run tuning
class AgentRuntimeConfig(StrictConfigModel):
    subordinate_max_turns: int = Field(default=1000, ge=1)


# global config
class GlobalConfig(StrictConfigModel):
    system: SystemConfig = Field(default_factory=SystemConfig)
    database: DatabaseConfig = Field(default_factory=DatabaseConfig)
    agents: dict[str, AgentConfig] = Field(default_factory=dict)
    agent_pool: AgentPoolConfig = Field(default_factory=AgentPoolConfig)
    agent_runtime: AgentRuntimeConfig = Field(default_factory=AgentRuntimeConfig)


###
# global config instance
###
_cfg: GlobalConfig = GlobalConfig()


def load_config():
    """load config from json file"""
    global _cfg

    config_file = WORKSPACE / "config.json"
    with open(config_file, "r", encoding="utf-8") as f:
        data = json.load(f)
    
    next_cfg = GlobalConfig.model_validate(data)
    for field_name in type(_cfg).model_fields:
        setattr(_cfg, field_name, getattr(next_cfg, field_name))


def get_config():
    """get config instance"""
    global _cfg
    return _cfg
