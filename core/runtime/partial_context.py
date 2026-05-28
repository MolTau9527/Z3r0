from dataclasses import dataclass
from typing import Any

from agents import TResponseInputItem

from core.conversation.store import Z3r0Session
from logger import get_logger
from schema.agent.events import (
    AgentEventSchema,
    TextCompleteEvent,
    TextDeltaEvent,
    ThinkingCompleteEvent,
    ThinkingDeltaEvent,
)


logger = get_logger(__name__)
_DELTA_TYPES: tuple[type, ...] = (TextDeltaEvent, ThinkingDeltaEvent)
_COMPLETE_TYPES: tuple[type, ...] = (TextCompleteEvent, ThinkingCompleteEvent)


@dataclass
class DeltaBuffer:
    is_thinking: bool
    segment_id: str
    content: str = ""
    complete: bool = False


def track_delta(buffers: dict[str, DeltaBuffer], event: AgentEventSchema) -> None:
    if isinstance(event, _DELTA_TYPES):
        buf = buffers.get(event.segment_id)
        if buf is None:
            buf = DeltaBuffer(is_thinking=isinstance(event, ThinkingDeltaEvent), segment_id=event.segment_id)
            buffers[event.segment_id] = buf
        buf.content += event.delta
    elif isinstance(event, _COMPLETE_TYPES):
        buf = buffers.get(event.segment_id)
        if buf is None:
            buf = DeltaBuffer(is_thinking=isinstance(event, ThinkingCompleteEvent), segment_id=event.segment_id)
            buffers[event.segment_id] = buf
        buf.content = event.text
        buf.complete = True


async def flush_partial_context(
    result: Any,
    memory_session: Z3r0Session,
    buffers: dict[str, DeltaBuffer],
    *,
    log_label: str,
) -> None:
    if result is None or getattr(result, "is_complete", True):
        return
    try:
        result.cancel(mode="immediate")
    except Exception:
        logger.warning("failed to cancel %s SDK stream", log_label, exc_info=True)
    items: list[TResponseInputItem] = [
        _partial_reasoning_item(buf) if buf.is_thinking else _partial_assistant_item(buf)
        for buf in buffers.values() if buf.content
    ]
    if not items:
        return
    try:
        await memory_session.add_items(items)
    except Exception:
        logger.warning("failed to inject partial %s context", log_label, exc_info=True)
    else:
        buffers.clear()


def _partial_assistant_item(buf: DeltaBuffer) -> TResponseInputItem:
    return {
        "id": f"partial_{buf.segment_id}",
        "type": "message",
        "role": "assistant",
        "content": [{"type": "output_text", "text": buf.content, "annotations": []}],
        "status": "completed" if buf.complete else "incomplete",
    }


def _partial_reasoning_item(buf: DeltaBuffer) -> TResponseInputItem:
    return {
        "id": f"partial_{buf.segment_id}",
        "type": "reasoning",
        "summary": [{"type": "summary_text", "text": buf.content}],
        "status": "completed" if buf.complete else "incomplete",
    }
