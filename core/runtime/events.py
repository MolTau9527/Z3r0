"""Normalize SDK stream events and stored items into our wire schema."""

import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from agents.items import ToolCallItem, ToolCallOutputItem
from agents.stream_events import RawResponsesStreamEvent, RunItemStreamEvent
from openai.types.responses.response_completed_event import ResponseCompletedEvent
from openai.types.responses.response_created_event import ResponseCreatedEvent
from openai.types.responses.response_error_event import ResponseErrorEvent
from openai.types.responses.response_failed_event import ResponseFailedEvent
from openai.types.responses.response_incomplete_event import ResponseIncompleteEvent
from openai.types.responses.response_reasoning_summary_text_delta_event import (
    ResponseReasoningSummaryTextDeltaEvent,
)
from openai.types.responses.response_reasoning_summary_text_done_event import (
    ResponseReasoningSummaryTextDoneEvent,
)
from openai.types.responses.response_reasoning_text_delta_event import ResponseReasoningTextDeltaEvent
from openai.types.responses.response_reasoning_text_done_event import ResponseReasoningTextDoneEvent
from openai.types.responses.response_text_delta_event import ResponseTextDeltaEvent
from openai.types.responses.response_text_done_event import ResponseTextDoneEvent
from pydantic import BaseModel

from core import TEXT_CONTENT_TYPES, is_internal_user_text
from core.runtime.input_items import display_text_from_content
from schema.agent.events import (
    AgentImageDetailSchema,
    AgentImageInputPart,
    AgentImageMediaTypeSchema,
    AgentEventSchema,
    AgentInputPart,
    AgentTextInputPart,
    ErrorEvent,
    TextCompleteEvent,
    TextDeltaEvent,
    ThinkingCompleteEvent,
    ThinkingDeltaEvent,
    ToolCallEvent,
    ToolResultEvent,
    SubagentTaskEvent,
    TurnBoundaryEvent,
    UserMessageEvent,
)
from schema.agent.subordinates import AgentSubordinateStatus


# `incomplete` is a partial output, not an error
_TOOL_ERROR_STATUSES = {"failed", "error"}

_RUN_ITEM_RESPONSE_BOUNDARIES = {
    "message_output_created",
    "reasoning_item_created",
    "tool_called",
    "tool_output",
    "tool_search_called",
    "tool_search_output_created",
    "handoff_requested",
    "handoff_occured",
    "mcp_approval_requested",
    "mcp_approval_response",
    "mcp_list_tools",
}


@dataclass(frozen=True, slots=True)
class _StreamSegmentKey:
    kind: str
    response_index: int = 0
    output_index: int = -1
    content_index: int = -1
    summary_index: int = -1


@dataclass(slots=True)
class _StreamSegment:
    segment_id: str
    complete: bool = False


class SdkStreamEventNormalizer:
    """Map SDK stream events to the public app-level event contract."""

    def __init__(self) -> None:
        self._segments: dict[_StreamSegmentKey, _StreamSegment] = {}
        self._next_segment_index = 1
        self._response_index = 0
        self._response_open = False

    def event_from_sdk_stream(self, sdk_event: Any, current_agent: str) -> AgentEventSchema | None:
        created_at = datetime.now()
        if isinstance(sdk_event, RawResponsesStreamEvent):
            if self._handle_response_lifecycle(sdk_event.data):
                return None
            return _from_raw_response(sdk_event.data, current_agent, created_at, self)
        if isinstance(sdk_event, RunItemStreamEvent):
            event = _from_run_item(sdk_event, current_agent, created_at)
            if sdk_event.name in _RUN_ITEM_RESPONSE_BOUNDARIES:
                self._response_open = False
            return event
        return None

    @property
    def response_index(self) -> int:
        if not self._response_open:
            return self._begin_response()
        return self._response_index

    def segment_id(self, key: _StreamSegmentKey, *, complete: bool) -> str:
        segment = self._segments.get(key)
        if segment is None or (segment.complete and not complete):
            segment = _StreamSegment(segment_id=f"{key.kind}_{self._next_segment_index}")
            self._segments[key] = segment
            self._next_segment_index += 1
        if complete:
            segment.complete = True
        return segment.segment_id

    def _handle_response_lifecycle(self, data: Any) -> bool:
        if isinstance(data, ResponseCreatedEvent):
            self._begin_response()
            return True
        if isinstance(data, (ResponseCompletedEvent, ResponseFailedEvent, ResponseIncompleteEvent)):
            self._response_open = False
            return True
        return False

    def _begin_response(self) -> int:
        self._response_index += 1
        self._response_open = True
        return self._response_index


def events_from_sdk_message(
    message: Any,
    fallback_id: str,
    *,
    created_at: datetime,
    owner_code: str = "",
    agent_name: str = "",
    nested_for: str = "",
    nested_call_id: str = "",
) -> list[AgentEventSchema]:
    if not isinstance(message, dict):
        return []

    scope = _scope(agent_name, nested_for, nested_call_id)
    segment_base = f"stored_{fallback_id}"
    match message.get("type"):
        case "message":
            return _events_from_stored_message(
                message,
                segment_base,
                created_at,
                scope,
                owner_code,
            )
        case "reasoning":
            return _events_from_stored_reasoning(message, segment_base, created_at, scope)
        case "function_call":
            return [ToolCallEvent(
                created_at=created_at,
                call_id=str(message.get("call_id") or message.get("id") or ""),
                name=str(message.get("name") or ""),
                arguments=_parse_tool_arguments(message.get("arguments")),
                **scope,
            )]
        case "function_call_output":
            return [ToolResultEvent(
                created_at=created_at,
                call_id=str(message.get("call_id") or ""),
                output=_normalize_to_str(message.get("output")),
                is_error=_is_tool_error(message.get("status")),
                **scope,
            )]
    return []


def event_from_subagent_task(
    *,
    run_id: str,
    parent_agent_code: str,
    parent_agent_instance_id: str = "",
    agent_code: str,
    agent_name: str,
    status: AgentSubordinateStatus,
    result: str = "",
    error: str = "",
    progress: str = "",
    nested_call_id: str = "",
    created_at: datetime | None = None,
) -> SubagentTaskEvent:
    return SubagentTaskEvent(
        created_at=created_at or datetime.now(),
        agent_name=agent_name,
        nested_for=parent_agent_code,
        nested_call_id=nested_call_id,
        run_id=run_id,
        parent_agent_code=parent_agent_code,
        parent_agent_instance_id=parent_agent_instance_id,
        agent_code=agent_code,
        status=status,
        result=result,
        error=error,
        progress=progress,
    )


def _scope(agent_name: str, nested_for: str, nested_call_id: str) -> dict[str, str]:
    return {
        "agent_name": agent_name,
        "nested_for": nested_for,
        "nested_call_id": nested_call_id,
    }


def _from_raw_response(
    data: Any,
    current_agent: str,
    created_at: datetime,
    normalizer: SdkStreamEventNormalizer,
) -> AgentEventSchema | None:
    if isinstance(data, ResponseTextDeltaEvent):
        return TextDeltaEvent(
            created_at=created_at,
            agent_name=current_agent,
            segment_id=normalizer.segment_id(_text_segment_key(data, normalizer.response_index), complete=False),
            delta=data.delta,
        )
    if isinstance(data, ResponseTextDoneEvent):
        return TextCompleteEvent(
            created_at=created_at,
            agent_name=current_agent,
            segment_id=normalizer.segment_id(_text_segment_key(data, normalizer.response_index), complete=True),
            text=data.text,
        )
    if isinstance(data, ResponseReasoningTextDeltaEvent):
        return ThinkingDeltaEvent(
            created_at=created_at,
            agent_name=current_agent,
            segment_id=normalizer.segment_id(_thinking_text_segment_key(data, normalizer.response_index), complete=False),
            delta=data.delta,
        )
    if isinstance(data, ResponseReasoningTextDoneEvent):
        return ThinkingCompleteEvent(
            created_at=created_at,
            agent_name=current_agent,
            segment_id=normalizer.segment_id(_thinking_text_segment_key(data, normalizer.response_index), complete=True),
            text=data.text,
        )
    if isinstance(data, ResponseReasoningSummaryTextDeltaEvent):
        return ThinkingDeltaEvent(
            created_at=created_at,
            agent_name=current_agent,
            segment_id=normalizer.segment_id(_thinking_summary_segment_key(data, normalizer.response_index), complete=False),
            delta=data.delta,
        )
    if isinstance(data, ResponseReasoningSummaryTextDoneEvent):
        return ThinkingCompleteEvent(
            created_at=created_at,
            agent_name=current_agent,
            segment_id=normalizer.segment_id(_thinking_summary_segment_key(data, normalizer.response_index), complete=True),
            text=data.text,
        )
    if isinstance(data, ResponseErrorEvent):
        return ErrorEvent(created_at=created_at, agent_name=current_agent, message=data.message, code=data.code or "")
    return None


def _from_run_item(event: RunItemStreamEvent, current_agent: str, created_at: datetime) -> AgentEventSchema | None:
    item = event.item
    agent_name = item.agent.name if item.agent is not None else current_agent

    if event.name == "tool_called" and isinstance(item, ToolCallItem):
        raw = item.raw_item
        return ToolCallEvent(
            created_at=created_at,
            agent_name=agent_name,
            call_id=_read_field(raw, "call_id") or _read_field(raw, "id") or "",
            name=_read_field(raw, "name") or item.title or "",
            arguments=_parse_tool_arguments(_read_field(raw, "arguments")),
        )
    if event.name == "tool_output" and isinstance(item, ToolCallOutputItem):
        raw = item.raw_item
        return ToolResultEvent(
            created_at=created_at,
            agent_name=agent_name,
            call_id=_read_field(raw, "call_id") or "",
            output=_normalize_to_str(item.output),
            is_error=_is_tool_error(_read_field(raw, "status")),
        )
    return None


def _events_from_stored_message(
    message: dict[str, Any], segment_base: str, created_at: datetime, scope: dict[str, str], owner_code: str,
) -> list[AgentEventSchema]:
    parts = _stored_message_text_parts(message.get("content"))
    text = "".join(parts)
    role = message.get("role")
    if role == "user":
        content_parts = _stored_user_input_parts(message.get("content"))
        if not content_parts:
            return []
        if _is_hidden_user_message(text, scope):
            return [TurnBoundaryEvent(created_at=created_at, **scope)]
        return [UserMessageEvent(
            created_at=created_at,
            content=content_parts,
            display_text=display_text_from_content(content_parts),
            target_agent_code=owner_code,
        )]
    if not text:
        return []
    if role == "assistant":
        return [
            TextCompleteEvent(
                created_at=created_at,
                segment_id=_stored_segment_id(segment_base, "text", index),
                text=part,
                **scope,
            )
            for index, part in enumerate(parts)
        ]
    return []


def _is_hidden_user_message(text: str, scope: dict[str, str]) -> bool:
    return is_internal_user_text(text) or bool(scope.get("nested_for"))


def _events_from_stored_reasoning(
    message: dict[str, Any],
    segment_base: str,
    created_at: datetime,
    scope: dict[str, str],
) -> list[AgentEventSchema]:
    return [
        ThinkingCompleteEvent(
            created_at=created_at,
            segment_id=_stored_segment_id(segment_base, f"thinking_{kind}", index),
            text=text,
            **scope,
        )
        for kind, index, text in _stored_reasoning_parts(message)
    ]


def _stored_message_text_parts(content: Any) -> list[str]:
    if isinstance(content, str):
        return [content] if content else []
    if not isinstance(content, list):
        return []
    parts: list[str] = []
    for item in content:
        if not isinstance(item, dict):
            continue
        text = item.get("text")
        if isinstance(text, str) and item.get("type") in TEXT_CONTENT_TYPES:
            parts.append(text)
    return [part for part in parts if part]


def _stored_user_input_parts(content: Any) -> list[AgentInputPart]:
    if isinstance(content, str):
        stripped = content.strip()
        return [AgentTextInputPart(text=stripped)] if stripped else []
    if not isinstance(content, list):
        return []
    parts: list[AgentInputPart] = []
    for item in content:
        if not isinstance(item, dict):
            continue
        item_type = item.get("type")
        if item_type in TEXT_CONTENT_TYPES:
            text = item.get("text")
            if isinstance(text, str) and text.strip():
                parts.append(AgentTextInputPart(text=text.strip()))
            continue
        if item_type == "input_image":
            image_part = _stored_image_part(item)
            if image_part is not None:
                parts.append(image_part)
    return parts


def _stored_image_part(item: dict[str, Any]) -> AgentImageInputPart | None:
    image_url = item.get("image_url")
    if not isinstance(image_url, str) or not image_url.startswith("data:"):
        return None
    header, separator, data = image_url.partition(",")
    if separator != "," or ";base64" not in header:
        return None
    media_type = header.removeprefix("data:").split(";", 1)[0]
    try:
        return AgentImageInputPart(
            media_type=AgentImageMediaTypeSchema(media_type),
            data=data,
            detail=AgentImageDetailSchema(str(item.get("detail") or "auto")),
        )
    except ValueError:
        return None


def _stored_reasoning_parts(message: dict[str, Any]) -> list[tuple[str, int, str]]:
    parts: list[tuple[str, int, str]] = []
    parts.extend(_stored_text_entries(message.get("content"), "content"))
    parts.extend(_stored_text_entries(message.get("summary"), "summary"))
    return parts


def _stored_text_entries(value: Any, kind: str) -> list[tuple[str, int, str]]:
    if isinstance(value, str):
        return [(kind, 0, value)] if value else []
    if not isinstance(value, list):
        return []
    entries: list[tuple[str, int, str]] = []
    for index, item in enumerate(value):
        if not isinstance(item, dict):
            continue
        text = item.get("text")
        if isinstance(text, str) and text:
            entries.append((kind, index, text))
    return entries


def _stored_segment_id(segment_base: str, kind: str, index: int) -> str:
    return f"{segment_base}_{kind}_{index}"


def _text_segment_key(data: Any, response_index: int) -> _StreamSegmentKey:
    return _segment_key("text", data, response_index=response_index, content_index=_int_attr(data, "content_index"))


def _thinking_text_segment_key(data: Any, response_index: int) -> _StreamSegmentKey:
    return _segment_key("thinking", data, response_index=response_index, content_index=_int_attr(data, "content_index"))


def _thinking_summary_segment_key(data: Any, response_index: int) -> _StreamSegmentKey:
    return _segment_key("thinking", data, response_index=response_index, summary_index=_int_attr(data, "summary_index"))


def _segment_key(
    kind: str,
    data: Any,
    *,
    response_index: int = 0,
    content_index: int = -1,
    summary_index: int = -1,
) -> _StreamSegmentKey:
    return _StreamSegmentKey(
        kind=kind,
        response_index=response_index,
        output_index=_int_attr(data, "output_index"),
        content_index=content_index,
        summary_index=summary_index,
    )


def _int_attr(data: Any, key: str) -> int:
    value = getattr(data, key, -1)
    if isinstance(value, int):
        return value
    try:
        return int(value)
    except (TypeError, ValueError):
        return -1


def _parse_tool_arguments(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    if not isinstance(raw, str) or not raw:
        return {}
    try:
        decoded = json.loads(raw)
    except json.JSONDecodeError:
        return {"_raw": raw}
    return decoded if isinstance(decoded, dict) else {"_value": decoded}


def _normalize_to_str(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, BaseModel):
        return value.model_dump_json()
    try:
        return json.dumps(value, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        return str(value)


def _is_tool_error(status: Any) -> bool:
    return isinstance(status, str) and status.lower() in _TOOL_ERROR_STATUSES


def _read_field(raw: Any, key: str) -> Any:
    if isinstance(raw, dict):
        return raw.get(key)
    return getattr(raw, key, None)
