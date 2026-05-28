"""Agent runtime package."""

import asyncio

from logger import get_logger
from schema.agent.events import AgentEventSchema

logger = get_logger(__name__)


def put_nowait_drop_oldest(queue: asyncio.Queue[AgentEventSchema], event: AgentEventSchema) -> None:
    """Best-effort enqueue: drop the oldest item when full so slow subscribers never block."""
    try:
        queue.put_nowait(event)
        return
    except asyncio.QueueFull:
        pass
    try:
        queue.get_nowait()
    except asyncio.QueueEmpty:
        pass
    try:
        queue.put_nowait(event)
    except asyncio.QueueFull:
        logger.debug("agent event dropped for slow subscriber")
