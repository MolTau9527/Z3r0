from schema.common.responses import CommonResponse
from schema.system_config.config import (
    UpdateInstanceConfigRequest,
    UpdateInstanceConfigResponse,
)
from service.system_config.config import (
    get_instance_config,
    update_instance_config,
)


async def get_instance_config_handler() -> CommonResponse:
    config = await get_instance_config()
    return CommonResponse(data=config)


async def update_instance_config_handler(request: UpdateInstanceConfigRequest) -> CommonResponse:
    config = await update_instance_config(request)
    return CommonResponse(data=UpdateInstanceConfigResponse(config=config, restarted=True))

