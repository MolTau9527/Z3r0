import asyncio
from http import HTTPStatus

from fastapi import HTTPException

from config import GlobalConfig, get_config, load_config, read_config_file, write_config_file
from core.delegation.subagents import start_subagent_runtime, stop_subagent_runtime
from core.runtime.session import AgentSessionPool, get_agent_pool, replace_agent_pool
from logger import get_logger
from schema.system_config.config import InstanceConfigSchema, UpdateInstanceConfigRequest


logger = get_logger(__name__)

_config_lock = asyncio.Lock()


async def get_instance_config() -> InstanceConfigSchema:
    cfg = get_config()
    return _instance_config_from_global(cfg)


async def update_instance_config(request: UpdateInstanceConfigRequest) -> InstanceConfigSchema:
    async with _config_lock:
        current = read_config_file()
        if set(request.agents) != set(current.agents):
            raise HTTPException(
                status_code=HTTPStatus.BAD_REQUEST.value,
                detail="agent set cannot be changed",
            )

        agents = {}
        for code, agent in current.agents.items():
            patch = request.agents[code]
            agents[code] = agent.model_copy(update={
                "name": patch.name,
                "description": patch.description,
                "base_url": patch.base_url,
                "api_key": patch.api_key,
                "model": patch.model,
                "use_responses": patch.use_responses,
                "context_window": patch.context_window,
            })

        next_cfg = current.model_copy(update={
            "agents": agents,
            "agent_pool": request.agent_pool,
            "agent_runtime": request.agent_runtime,
        })
        write_config_file(next_cfg)
        load_config()
        await rebuild_agent_instances()
        logger.info("instance config updated and agent instances rebuilt")
        return _instance_config_from_global(get_config())


async def rebuild_agent_instances() -> None:
    old_pool = get_agent_pool()
    await stop_subagent_runtime()
    await old_pool.stop()
    new_pool = replace_agent_pool(AgentSessionPool())
    await start_subagent_runtime()
    await new_pool.start()


def _instance_config_from_global(cfg: GlobalConfig) -> InstanceConfigSchema:
    return InstanceConfigSchema(
        agents=cfg.agents,
        agent_pool=cfg.agent_pool,
        agent_runtime=cfg.agent_runtime,
    )
