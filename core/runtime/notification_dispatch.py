"""Runtime wakeup and dispatch helpers for durable agent notifications."""

from __future__ import annotations

import asyncio

from core.runtime.context import AgentRuntimeContext, MAIN_AGENT_INSTANCE_PREFIX
from logger import get_logger


logger = get_logger(__name__)
_target_signal_lock = asyncio.Lock()
_target_signals: dict[str, asyncio.Event] = {}
_target_signal_versions: dict[str, int] = {}


def is_main_agent_instance(agent_instance_id: str) -> bool:
    return agent_instance_id.startswith(MAIN_AGENT_INSTANCE_PREFIX)


async def signal_target_notifications(target_agent_instance_id: str) -> None:
    async with _target_signal_lock:
        _target_signal_versions[target_agent_instance_id] = _target_signal_versions.get(target_agent_instance_id, 0) + 1
        signal = _target_signals.get(target_agent_instance_id)
        if signal is not None:
            signal.set()


async def target_notification_version(target_agent_instance_id: str) -> int:
    async with _target_signal_lock:
        return _target_signal_versions.get(target_agent_instance_id, 0)


async def wait_for_target_notifications(
    target_agent_instance_id: str,
    *,
    after_version: int | None = None,
    timeout_seconds: float | None = None,
) -> bool:
    async with _target_signal_lock:
        version = _target_signal_versions.get(target_agent_instance_id, 0) if after_version is None else after_version
        signal = _target_signals.setdefault(target_agent_instance_id, asyncio.Event())

    while True:
        async with _target_signal_lock:
            if _target_signal_versions.get(target_agent_instance_id, 0) != version:
                return True
            signal = _target_signals[target_agent_instance_id]
            signal.clear()
        try:
            await asyncio.wait_for(signal.wait(), timeout=timeout_seconds)
        except TimeoutError:
            return False


async def forget_target_notifications(target_agent_instance_id: str) -> None:
    async with _target_signal_lock:
        _target_signals.pop(target_agent_instance_id, None)
        _target_signal_versions.pop(target_agent_instance_id, None)


async def dispatch_target_notifications(
    context: AgentRuntimeContext,
    *,
    target_agent_instance_id: str,
) -> bool:
    await signal_target_notifications(target_agent_instance_id)
    if not is_main_agent_instance(target_agent_instance_id):
        return False
    if target_agent_instance_id != context.agent_instance_id:
        logger.warning(
            "refusing to drain notifications for mismatched main agent target: context=%s target=%s session=%s",
            context.agent_instance_id,
            target_agent_instance_id,
            context.session_id,
        )
        return False
    return await drain_target_notifications(
        context,
        target_agent_instance_id=target_agent_instance_id,
    )


async def drain_target_notifications(
    context: AgentRuntimeContext,
    *,
    target_agent_instance_id: str,
) -> bool:
    from core.runtime.session import get_agent_pool

    return await get_agent_pool().drain_notifications(
        context.session_id,
        context,
        target_agent_instance_id=target_agent_instance_id,
    )


async def drain_main_agent_notifications_from_session(
    *,
    session_id: str,
    target_agent_instance_id: str,
    agent_code: str,
    sandbox_container_id: int | None,
    nested_for_agent_code: str = "",
    nested_call_id: str = "",
) -> bool:
    await signal_target_notifications(target_agent_instance_id)
    if not is_main_agent_instance(target_agent_instance_id):
        return False

    from middleware.auth import AuthUser
    from service.agent import sessions as agent_sessions
    from service.agent.runtime import build_runtime_context
    from service.system_user.users import query_system_user_by_id

    session = await agent_sessions.get_session_meta(session_id)
    if session is None:
        return False
    user = await query_system_user_by_id(session.owner_id)
    if user is None:
        return False

    auth_user = AuthUser(id=user.id, role=user.role, email=user.email, username=user.username)
    context = await build_runtime_context(session_id, auth_user, sandbox_container_id, agent_code)
    context.agent_instance_id = target_agent_instance_id
    context.nested_for_agent_code = nested_for_agent_code
    context.nested_call_id = nested_call_id
    return await drain_target_notifications(
        context,
        target_agent_instance_id=target_agent_instance_id,
    )
