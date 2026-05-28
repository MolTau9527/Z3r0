/**
 * Unified event-content-key utilities for deduplicating agent stream events.
 *
 * Used in two scenarios:
 * 1. Filtering buffered live events against a freshly-loaded history page
 *    (history-vs-live overlap during reconnect / initial load).
 * 2. Defence-in-depth inside the chat-state reducer to skip events whose
 *    semantic content is already present in the current transcript.
 *
 * The approach is deliberately SDK-agnostic: keys are computed purely from
 * the normalised AgentStreamEvent fields (type, nested scope, natural IDs,
 * and content text).
 */
import type { AgentStreamEvent } from "../../shared/api/types";
import { stableJson } from "../../shared/lib/json";

const SEP = "\x1f";

// ---------------------------------------------------------------------------
// History coverage — built once from the history page, queried per buffered event
// ---------------------------------------------------------------------------

type HistoryCoverage = {
  /** Complete text/thinking content grouped by (type, nested_for, nested_call_id). */
  completeTextsByScope: Map<string, string[]>;
  /** tool_call identity keys (by call_id AND semantic name+args). */
  toolCalls: Set<string>;
  /** tool_result identity keys (by call_id). */
  toolResults: Set<string>;
  /** subagent_task identity keys (by run_id). */
  subagents: Set<string>;
};

function textScopeKey(kind: "text" | "thinking", nestedFor: string, nestedCallId: string): string {
  return `${kind}${SEP}${nestedFor}${SEP}${nestedCallId}`;
}

function toolIdKey(nestedFor: string, nestedCallId: string, callId: string): string {
  return `${nestedFor}${SEP}${nestedCallId}${SEP}${callId}`;
}

function toolSemanticKey(nestedFor: string, nestedCallId: string, name: string, args: Record<string, unknown>): string {
  return `sem${SEP}${nestedFor}${SEP}${nestedCallId}${SEP}${name}${SEP}${stableJson(args)}`;
}

function toolResultKey(nestedFor: string, nestedCallId: string, callId: string, output: string, isError: boolean): string {
  return `${nestedFor}${SEP}${nestedCallId}${SEP}${callId}${SEP}${output}${SEP}${isError ? "1" : "0"}`;
}

function subagentKey(event: Extract<AgentStreamEvent, { type: "subagent_task" }>): string {
  return [
    event.run_id,
    event.status,
    event.progress,
    event.result,
    event.error,
  ].join(SEP);
}

function buildHistoryCoverage(events: readonly AgentStreamEvent[]): HistoryCoverage {
  const completeTextsByScope = new Map<string, string[]>();
  const toolCalls = new Set<string>();
  const toolResults = new Set<string>();
  const subagents = new Set<string>();

  for (const event of events) {
    switch (event.type) {
      case "text_complete":
      case "thinking_complete": {
        const scope = textScopeKey(
          event.type === "text_complete" ? "text" : "thinking",
          event.nested_for,
          event.nested_call_id,
        );
        const texts = completeTextsByScope.get(scope);
        if (texts) texts.push(event.text);
        else completeTextsByScope.set(scope, [event.text]);
        break;
      }
      case "tool_call":
        toolCalls.add(toolIdKey(event.nested_for, event.nested_call_id, event.call_id));
        toolCalls.add(toolSemanticKey(event.nested_for, event.nested_call_id, event.name, event.arguments ?? {}));
        break;
      case "tool_result":
        toolResults.add(toolResultKey(
          event.nested_for,
          event.nested_call_id,
          event.call_id,
          event.output,
          event.is_error,
        ));
        break;
      case "subagent_task":
        subagents.add(subagentKey(event));
        break;
    }
  }

  return { completeTextsByScope, toolCalls, toolResults, subagents };
}

function isTextCoveredByHistory(
  kind: "text" | "thinking",
  nestedFor: string,
  nestedCallId: string,
  text: string,
  coverage: HistoryCoverage,
): boolean {
  if (!text) return false;
  const candidates = coverage.completeTextsByScope.get(textScopeKey(kind, nestedFor, nestedCallId));
  if (!candidates) return false;
  return candidates.some((c) => c === text || c.startsWith(text));
}

function isHistoryCovered(event: AgentStreamEvent, coverage: HistoryCoverage): boolean {
  switch (event.type) {
    case "text_complete":
      return isTextCoveredByHistory("text", event.nested_for, event.nested_call_id, event.text, coverage);
    case "thinking_complete":
      return isTextCoveredByHistory("thinking", event.nested_for, event.nested_call_id, event.text, coverage);
    case "text_delta":
      return isTextCoveredByHistory("text", event.nested_for, event.nested_call_id, event.delta, coverage);
    case "thinking_delta":
      return isTextCoveredByHistory("thinking", event.nested_for, event.nested_call_id, event.delta, coverage);
    case "tool_call":
      return coverage.toolCalls.has(toolIdKey(event.nested_for, event.nested_call_id, event.call_id))
        || coverage.toolCalls.has(toolSemanticKey(event.nested_for, event.nested_call_id, event.name, event.arguments ?? {}));
    case "tool_result":
      return coverage.toolResults.has(toolResultKey(
        event.nested_for,
        event.nested_call_id,
        event.call_id,
        event.output,
        event.is_error,
      ));
    case "subagent_task":
      return coverage.subagents.has(subagentKey(event));
    default:
      return false;
  }
}

// ---------------------------------------------------------------------------
// Public API — single function replaces filterBufferedHistoryOverlap + helpers
// ---------------------------------------------------------------------------

export function filterOverlappingEvents(
  historyEvents: readonly AgentStreamEvent[],
  bufferedEvents: readonly AgentStreamEvent[],
): AgentStreamEvent[] {
  if (!bufferedEvents.length) return [];
  const coverage = buildHistoryCoverage(historyEvents);
  return bufferedEvents.filter((event) => !isHistoryCovered(event, coverage));
}
