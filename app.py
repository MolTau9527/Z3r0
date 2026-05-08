from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from agents import set_tracing_disabled
from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException

from config import ROOT_PATH, get_config
from core.subordinates import start_subagent_runtime, stop_subagent_runtime
from core.runtime import get_agent_pool
from database import close_engine, create_all_tables, init_engine
from logger import get_logger
from middleware.auth import JwtAuthMiddleware
from middleware.response import (
    CommonResponseStatusMiddleware,
    http_exception_handler,
    request_validation_exception_handler,
)
from router.agent_router import router as agent_router
from router.agent_session_router import router as agent_session_router
from router.fallback_router import api_not_found_router
from router.sandbox_container_router import router as sandbox_container_router
from router.sandbox_image_router import router as sandbox_image_router
from router.system_user_router import router as system_user_router
from router.work_project_router import router as work_project_router
from schema.system_user_schema import SystemUserRole
from service.sandbox_container_service import (
    invalidate_all_agent_tool_bindings,
    start_sandbox_container_status_monitor,
    stop_sandbox_container_status_monitor,
)
from service.system_user_service import create_system_user, query_system_user_by_username
from utils.urllib3_compat import install_urllib3_closed_file_close_patch


logger = get_logger(__name__)

install_urllib3_closed_file_close_patch()

WEB_DIST_PATH = ROOT_PATH / "web" / "dist"
API_PREFIX = "/api"


async def _bootstrap_admin_user() -> None:
    bootstrap = get_config().system.bootstrap_admin
    if not bootstrap.enabled:
        logger.info("bootstrap admin user skipped")
        return

    if await query_system_user_by_username(bootstrap.username) is not None:
        logger.info("bootstrap admin user already exists: %s", bootstrap.username)
        return

    await create_system_user(
        username=bootstrap.username,
        password=bootstrap.password,
        email=bootstrap.email,
        role=SystemUserRole.ADMIN,
    )
    logger.info("bootstrap admin user created: %s", bootstrap.username)


def _mount_frontend(app: FastAPI) -> None:
    """serve built frontend assets when web/dist exists"""
    index_path = WEB_DIST_PATH / "index.html"
    if not index_path.is_file():
        logger.info("frontend static route skipped: %s not found", index_path)
        return

    assets_path = WEB_DIST_PATH / "assets"
    if assets_path.is_dir():
        app.mount("/assets", StaticFiles(directory=assets_path), name="web-assets")

    async def serve_frontend(path: str = "") -> FileResponse:
        return FileResponse(index_path)

    app.add_api_route("/", serve_frontend, methods=["GET"], include_in_schema=False)
    app.add_api_route("/{path:path}", serve_frontend, methods=["GET"], include_in_schema=False)


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncGenerator[None, None]:
    try:
        init_engine()
        await create_all_tables()
        await _bootstrap_admin_user()

        set_tracing_disabled(True)
        await start_subagent_runtime()
        await get_agent_pool().start()
        await start_sandbox_container_status_monitor()

        yield
    except Exception:
        # surface startup failures; the finally block below would otherwise hide them
        logger.exception("lifespan startup failed")
        raise
    finally:
        await stop_sandbox_container_status_monitor()
        await invalidate_all_agent_tool_bindings()
        await stop_subagent_runtime()
        await get_agent_pool().stop()
        await close_engine()


def create_app() -> FastAPI:
    app = FastAPI(title="Z3r0 - AI + Security Attack Platform", lifespan=lifespan)

    app.add_exception_handler(StarletteHTTPException, http_exception_handler)
    app.add_exception_handler(RequestValidationError, request_validation_exception_handler)
    logger.info("exception handlers added")

    app.add_middleware(CommonResponseStatusMiddleware)
    app.add_middleware(JwtAuthMiddleware)
    logger.info("middleware added")

    app.include_router(system_user_router, prefix=API_PREFIX)
    app.include_router(sandbox_image_router, prefix=API_PREFIX)
    app.include_router(sandbox_container_router, prefix=API_PREFIX)
    app.include_router(work_project_router, prefix=API_PREFIX)
    app.include_router(agent_router, prefix=API_PREFIX)
    app.include_router(agent_session_router, prefix=API_PREFIX)
    app.include_router(api_not_found_router, prefix=API_PREFIX)
    logger.info("api router added")

    _mount_frontend(app)
    return app
