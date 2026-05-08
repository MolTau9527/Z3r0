from agents.extensions.memory import SQLAlchemySession
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlmodel import SQLModel
from sqlmodel.ext.asyncio.session import AsyncSession

from config import get_config
from logger import get_logger
from model.agent_notification_model import AgentNotification
from model.agent_subordinate_model import AgentSubordinateTask
from model.agent_message_meta_model import AgentMessageMeta
from model.agent_session_meta_model import AgentSessionMeta
from model.sandbox_container_model import SandboxContainer
from model.sandbox_image_model import SandboxImage
from model.system_user_model import SystemUser
from model.work_project_model import WorkProject
from utils.sdk_tables import BOOTSTRAP_SESSION_ID


logger = get_logger(__name__)

# registered so SQLModel.metadata picks every table up at create_all time
_registered_models = [
    SystemUser, SandboxImage, SandboxContainer, WorkProject,
    AgentSessionMeta, AgentMessageMeta, AgentSubordinateTask, AgentNotification,
]

_engine: AsyncEngine | None = None


async def create_all_tables() -> None:
    global _engine
    if _engine is None:
        raise RuntimeError("database engine is not initialized")

    # SDK manages its own metadata; bootstrap a throwaway session to obtain it
    sdk_metadata = SQLAlchemySession(session_id=BOOTSTRAP_SESSION_ID, engine=_engine)._metadata

    # SDK tables first; some app tables (e.g. AgentMessageMeta) FK into them
    async with _engine.begin() as conn:
        await conn.run_sync(sdk_metadata.create_all)
        await conn.run_sync(SQLModel.metadata.create_all)

    logger.info("all tables created")


async def close_engine() -> None:
    global _engine
    if _engine is not None:
        await _engine.dispose()
        _engine = None


def init_engine() -> None:
    global _engine
    if _engine is not None:
        return

    cfg = get_config()
    dsn = f"postgresql+asyncpg://{cfg.database.username}:{cfg.database.password}@{cfg.database.host}:{cfg.database.port}/{cfg.database.database}"

    _engine = create_async_engine(url=dsn)
    logger.info("async postgres engine initialized")


def get_engine() -> AsyncEngine:
    global _engine
    if _engine is None:
        raise RuntimeError("database engine is not initialized")
    return _engine


def get_async_session() -> AsyncSession:
    return AsyncSession(get_engine())
