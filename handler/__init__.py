"""Shared handler utilities."""

import asyncio

from fastapi import WebSocket, status as ws_status


async def cancel_ws_task(task: asyncio.Task | None) -> None:
    """Cancel an asyncio task spawned by a WebSocket handler, draining any result."""
    if task is None:
        return
    if task.done():
        try:
            task.result()
        except (asyncio.CancelledError, Exception):
            pass
        return
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


async def close_ws_silently(websocket: WebSocket, code: int = ws_status.WS_1011_INTERNAL_ERROR) -> None:
    """Best-effort WebSocket close that never raises."""
    try:
        await websocket.close(code=code)
    except Exception:
        pass
