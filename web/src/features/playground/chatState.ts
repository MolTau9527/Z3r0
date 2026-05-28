import type {
  AgentContentEvent,
  AgentInputPart,
  AgentStreamEvent,
  SubagentTaskEvent,
  TextCompleteEvent,
  ThinkingCompleteEvent,
  ToolCallEvent,
  ToolResultEvent,
} from "../../shared/api/types";
import { stableJson } from "../../shared/lib/json";

export type ThinkingItem = {
  kind: "thinking";
  id: string;
  segmentId: ThinkingCompleteEvent["segment_id"];
  text: ThinkingCompleteEvent["text"];
  complete: boolean;
};

export type TextItem = {
  kind: "text";
  id: string;
  segmentId: TextCompleteEvent["segment_id"];
  text: TextCompleteEvent["text"];
  complete: boolean;
};

export type ToolExecutionItem = {
  kind: "tool";
  id: string;
  callId: ToolCallEvent["call_id"];
  name: ToolCallEvent["name"];
  arguments: NonNullable<ToolCallEvent["arguments"]>;
  output: ToolResultEvent["output"];
  isError: ToolResultEvent["is_error"];
  resolved: boolean;
  nested?: NestedTranscript;
  subagentTask?: SubagentExecutionItem;
};

export type SubagentExecutionItem = {
  kind: "subagent";
  id: SubagentTaskEvent["run_id"];
  createdAt: SubagentTaskEvent["created_at"];
  runId: SubagentTaskEvent["run_id"];
  parentAgentCode: SubagentTaskEvent["parent_agent_code"];
  parentAgentInstanceId: SubagentTaskEvent["parent_agent_instance_id"];
  agentCode: SubagentTaskEvent["agent_code"];
  nestedCallId: SubagentTaskEvent["nested_call_id"];
  status: SubagentTaskEvent["status"];
  result: SubagentTaskEvent["result"];
  error: SubagentTaskEvent["error"];
  progress: SubagentTaskEvent["progress"];
};

export type ErrorItem = { kind: "error"; id: string; message: string };
export type ExecutionItem = ToolExecutionItem | SubagentExecutionItem;
export type TranscriptBlock = ThinkingItem | TextItem | ExecutionItem | ErrorItem;

export type AgentTranscript = {
  createdAt: AgentContentEvent["created_at"] | "";
  agentName: string;
  blocks: TranscriptBlock[];
};

export type NestedTranscript = AgentTranscript;

export type ChatNode =
  | {
      kind: "user";
      id: string;
      createdAt: AgentContentEvent["created_at"];
      content: AgentInputPart[];
      displayText: string;
      targetAgentCode: string;
    }
  | ({ kind: "agent"; id: string } & AgentTranscript);

export type ChatState = {
  nodes: ChatNode[];
  streaming: boolean;
  pendingNested: Record<string, AgentContentEvent[]>;
  liveFrom: number | null;
};

export const initialChatState: ChatState = { nodes: [], streaming: false, pendingNested: {}, liveFrom: null };

type AgentNode = Extract<ChatNode, { kind: "agent" }>;
type StreamingItem = ThinkingItem | TextItem;

function newId() {
  if (typeof crypto !== "undefined" && typeof crypto.randomUUID === "function") {
    return crypto.randomUUID();
  }
  return `id-${Date.now().toString(36)}-${Math.random().toString(36).slice(2)}`;
}

function appendUserMessage(
  state: ChatState,
  content: AgentInputPart[],
  displayText: string,
  targetAgentCode: string,
  createdAt: AgentContentEvent["created_at"],
): ChatState {
  const signature = contentSignature(content);
  if (state.streaming) {
    const existingIndex = findLiveUserMessageIndex(state.nodes, signature, targetAgentCode);
    if (existingIndex !== -1) {
      const existing = state.nodes[existingIndex];
      const liveFrom = state.liveFrom ?? state.nodes.length;
      if (existing.kind !== "user" || !targetAgentCode || existing.targetAgentCode === targetAgentCode) {
        return { ...state, streaming: true, liveFrom };
      }
      const nodes = state.nodes.slice();
      nodes[existingIndex] = { ...existing, targetAgentCode };
      return { ...state, nodes, streaming: true, liveFrom };
    }
  }
  const lastNode = state.nodes[state.nodes.length - 1];
  if (lastNode?.kind === "user" && contentSignature(lastNode.content) === signature) {
    const liveFrom = state.liveFrom ?? state.nodes.length;
    if (!targetAgentCode || lastNode.targetAgentCode === targetAgentCode) {
      return { ...state, streaming: true, liveFrom };
    }
    const nodes = state.nodes.slice();
    nodes[nodes.length - 1] = { ...lastNode, targetAgentCode };
    return { ...state, nodes, streaming: true, liveFrom };
  }
  const nodes = [...state.nodes, {
    kind: "user" as const,
    id: newId(),
    createdAt,
    content,
    displayText,
    targetAgentCode,
  }];
  return { ...state, nodes, streaming: true, liveFrom: nodes.length };
}

function findLiveUserMessageIndex(nodes: ChatNode[], signature: string, targetAgentCode: string): number {
  for (let index = nodes.length - 1; index >= 0; index -= 1) {
    const node = nodes[index];
    if (node.kind !== "user") continue;
    if (contentSignature(node.content) !== signature) return -1;
    if (!targetAgentCode || !node.targetAgentCode || node.targetAgentCode === targetAgentCode) {
      return index;
    }
    return -1;
  }
  return -1;
}

export function finishChatTurn(state: ChatState): ChatState {
  return { ...state, streaming: false, liveFrom: null, pendingNested: prunePendingNested(state) };
}

export function disconnectChatTurn(state: ChatState): ChatState {
  if (!state.streaming) return state;
  const nodes = state.liveFrom === null ? state.nodes : state.nodes.slice(0, state.liveFrom);
  return { ...state, nodes, streaming: false, liveFrom: null, pendingNested: prunePendingNested({ ...state, nodes }) };
}

function chatReduce(state: ChatState, event: AgentContentEvent): ChatState {
  return applyContentEvent(state, event);
}

export function streamReduce(state: ChatState, event: AgentStreamEvent): ChatState {
  if (event.type === "done") return finishChatTurn(state);
  if (event.type === "run_state") return event.running ? startChatTurn(state) : finishChatTurn(state);
  if (stateHasContentEvent(state, event)) return state;
  return applyContentEvent(state, event);
}

export function chatReplay(events: readonly AgentContentEvent[]): ChatState {
  return finishChatTurn(events.reduce<ChatState>(chatReduce, initialChatState));
}

export function prependChatHistory(state: ChatState, events: readonly AgentContentEvent[]): ChatState {
  if (!events.length) return state;
  const prefix = chatReplay(events);
  if (!prefix.nodes.length) return state;
  return { ...state, nodes: mergeChatNodeBoundary(prefix.nodes, state.nodes) };
}

function mergeChatNodeBoundary(prefix: ChatNode[], suffix: ChatNode[]): ChatNode[] {
  if (!prefix.length) return suffix;
  if (!suffix.length) return prefix;
  const lastPrefix = prefix[prefix.length - 1];
  const firstSuffix = suffix[0];
  if (lastPrefix.kind !== "agent" || firstSuffix.kind !== "agent") {
    return [...prefix, ...suffix];
  }
  return [
    ...prefix.slice(0, -1),
    {
      ...firstSuffix,
      createdAt: lastPrefix.createdAt || firstSuffix.createdAt,
      agentName: firstSuffix.agentName || lastPrefix.agentName,
      blocks: [...lastPrefix.blocks, ...firstSuffix.blocks],
    },
    ...suffix.slice(1),
  ];
}

function startChatTurn(state: ChatState): ChatState {
  if (state.streaming && state.liveFrom !== null) return state;
  const tailIndex = state.nodes.length - 1;
  const tail = state.nodes[tailIndex];
  return { ...state, streaming: true, liveFrom: tail?.kind === "agent" ? tailIndex : state.nodes.length };
}

function applyContentEvent(state: ChatState, event: AgentContentEvent): ChatState {
  if (event.type === "user_message") {
    return appendUserMessage(state, event.content, event.display_text, event.target_agent_code, event.created_at);
  }
  if (event.type === "turn_boundary") {
    return event.nested_call_id ? state : finishChatTurn(state);
  }
  const nestedCallId = "nested_call_id" in event ? event.nested_call_id : "";
  return nestedCallId ? routeToNested(state, event, nestedCallId) : routeToTopLevel(state, event);
}

function routeToTopLevel(state: ChatState, event: AgentContentEvent): ChatState {
  const nodes = state.nodes.slice();
  const lastIndex = nodes.length - 1;
  const lastNode = nodes[lastIndex];
  let agent: AgentNode;
  if (isWritableAgentTail(state, lastNode, lastIndex)) {
    agent = cloneAgentNode(lastNode);
    if (!agent.createdAt) agent.createdAt = event.created_at;
    nodes[lastIndex] = agent;
  } else {
    const existingIndex = state.streaming ? findLiveAgentForEvent(nodes, state.liveFrom, event) : -1;
    if (existingIndex !== -1) {
      agent = cloneAgentNode(nodes[existingIndex] as AgentNode);
      if (!agent.createdAt) agent.createdAt = event.created_at;
      nodes[existingIndex] = agent;
    } else {
      agent = createAgentNode(event.created_at);
      nodes.push(agent);
    }
  }

  const finished = applyEventToTranscript(agent, event);
  const liveFrom = state.liveFrom ?? nodes.length - 1;
  const nextState = finished
    ? finishChatTurn({ ...state, nodes, liveFrom })
    : { ...state, nodes, streaming: true, liveFrom };
  if (event.type === "tool_call" || event.type === "tool_result") {
    return drainPendingNested(nextState, event.call_id);
  }
  if (event.type === "error") return clearPendingNested(nextState);
  return nextState;
}

function isWritableAgentTail(state: ChatState, node: ChatNode | undefined, index: number): node is AgentNode {
  return node?.kind === "agent" && state.streaming && state.liveFrom !== null && index >= state.liveFrom;
}

function findLiveAgentForEvent(nodes: ChatNode[], liveFrom: number | null, event: AgentContentEvent): number {
  const start = liveFrom ?? 0;
  for (let index = nodes.length - 1; index >= start; index -= 1) {
    const node = nodes[index];
    if (node.kind !== "agent") continue;
    if (transcriptHasEvent(node, event)) return index;
  }
  return -1;
}

function routeToNested(state: ChatState, event: AgentContentEvent, nestedCallId: string): ChatState {
  const routed = routeToNestedNow(state, event, nestedCallId);
  if (routed) return routed;
  const queued = state.pendingNested[nestedCallId] ?? [];
  return { ...state, pendingNested: { ...state.pendingNested, [nestedCallId]: [...queued, event] } };
}

function routeToNestedNow(state: ChatState, event: AgentContentEvent, nestedCallId: string): ChatState | null {
  if (event.type === "subagent_task") {
    return updateNestedTool(state, nestedCallId, (tool) => {
      tool.subagentTask = subagentExecutionItemFromEvent(event);
    });
  }
  return updateNestedTool(state, nestedCallId, (tool) => {
    const nested = tool.nested ? cloneTranscript(tool.nested) : createTranscript(event.created_at);
    if (!nested.createdAt) nested.createdAt = event.created_at;
    applyEventToTranscript(nested, event);
    tool.nested = nested;
  });
}

function updateNestedTool(state: ChatState, callId: string, update: (tool: ToolExecutionItem) => void): ChatState | null {
  const nodes = state.nodes.slice();
  for (let index = nodes.length - 1; index >= 0; index -= 1) {
    const node = nodes[index];
    if (node.kind !== "agent") continue;
    const blockIndex = findToolBlockIndex(node.blocks, callId);
    if (blockIndex === -1) continue;

    const agent = cloneAgentNode(node);
    const tool = { ...(agent.blocks[blockIndex] as ToolExecutionItem) };
    update(tool);
    agent.blocks[blockIndex] = tool;
    nodes[index] = agent;
    return { ...state, nodes };
  }
  return null;
}

function drainPendingNested(state: ChatState, callId: string): ChatState {
  const pending = state.pendingNested[callId];
  if (!pending?.length) return state;
  let nextState = state;
  const remaining: AgentContentEvent[] = [];
  for (const event of pending) {
    const routed = routeToNestedNow(nextState, event, callId);
    if (routed) nextState = routed;
    else remaining.push(event);
  }
  const pendingNested = { ...nextState.pendingNested };
  if (remaining.length) pendingNested[callId] = remaining;
  else delete pendingNested[callId];
  return { ...nextState, pendingNested };
}

function clearPendingNested(state: ChatState): ChatState {
  return Object.keys(state.pendingNested).length ? { ...state, pendingNested: {} } : state;
}

function prunePendingNested(state: ChatState): ChatState["pendingNested"] {
  if (!Object.keys(state.pendingNested).length) return state.pendingNested;
  const pendingNested: ChatState["pendingNested"] = {};
  for (const [callId, events] of Object.entries(state.pendingNested)) {
    if (!hasToolCall(state.nodes, callId)) pendingNested[callId] = events;
  }
  return pendingNested;
}

function hasToolCall(nodes: ChatNode[], callId: string): boolean {
  return nodes.some((node) => node.kind === "agent" && findToolBlockIndex(node.blocks, callId) !== -1);
}

function applyEventToTranscript(transcript: AgentTranscript, event: AgentContentEvent): boolean {
  switch (event.type) {
    case "user_message":
    case "turn_boundary":
      return false;
    case "thinking_delta":
      setAgentName(transcript, event.agent_name);
      upsertStreamingBlock(transcript.blocks, "thinking", event.segment_id, { delta: event.delta });
      return false;
    case "thinking_complete":
      setAgentName(transcript, event.agent_name);
      upsertStreamingBlock(transcript.blocks, "thinking", event.segment_id, { text: event.text, complete: true });
      return false;
    case "text_delta":
      setAgentName(transcript, event.agent_name);
      upsertStreamingBlock(transcript.blocks, "text", event.segment_id, { delta: event.delta });
      return false;
    case "text_complete":
      setAgentName(transcript, event.agent_name);
      upsertStreamingBlock(transcript.blocks, "text", event.segment_id, { text: event.text, complete: true });
      return false;
    case "tool_call":
      setAgentName(transcript, event.agent_name);
      upsertToolCall(transcript.blocks, event.call_id, event.name, event.arguments ?? {});
      return false;
    case "tool_result":
      setAgentName(transcript, event.agent_name);
      upsertToolResult(transcript.blocks, event.call_id, event.output, event.is_error);
      return false;
    case "subagent_task":
      setAgentName(transcript, event.agent_name);
      upsertSubagentTask(transcript.blocks, subagentExecutionItemFromEvent(event));
      return false;
    case "error":
      setAgentName(transcript, event.agent_name);
      transcript.blocks.push({ kind: "error", id: newId(), message: event.message || "agent run failed" });
      return true;
  }
}

function transcriptHasEvent(transcript: AgentTranscript, event: AgentContentEvent): boolean {
  switch (event.type) {
    case "thinking_delta":
      return hasCoveredCompletedText(transcript.blocks, "thinking", event.delta);
    case "thinking_complete":
      return hasCoveredCompletedText(transcript.blocks, "thinking", event.text);
    case "text_delta":
      return hasCoveredCompletedText(transcript.blocks, "text", event.delta);
    case "text_complete":
      return hasCoveredCompletedText(transcript.blocks, "text", event.text);
    case "tool_call":
      return findToolBlockIndex(transcript.blocks, event.call_id, event.name, event.arguments ?? {}) !== -1;
    case "tool_result": {
      const index = findToolBlockIndex(transcript.blocks, event.call_id);
      const block = index === -1 ? null : transcript.blocks[index];
      return Boolean(block?.kind === "tool" && block.resolved && block.output === event.output && block.isError === event.is_error);
    }
    case "subagent_task":
      return transcript.blocks.some((block) => (
        block.kind === "subagent"
        && block.runId === event.run_id
        && block.status === event.status
        && block.progress === event.progress
        && block.result === event.result
        && block.error === event.error
      ));
    default:
      return false;
  }
}

function stateHasContentEvent(state: ChatState, event: AgentContentEvent): boolean {
  if (event.type === "user_message") {
    const signature = contentSignature(event.content);
    return findLiveUserMessageIndex(state.nodes, signature, event.target_agent_code) !== -1;
  }
  const nestedCallId = "nested_call_id" in event ? event.nested_call_id : "";
  if (nestedCallId) return stateHasNestedEvent(state, nestedCallId, event);
  return liveNodes(state).some((node) => node.kind === "agent" && transcriptHasEvent(node, event));
}

function stateHasNestedEvent(state: ChatState, callId: string, event: AgentContentEvent): boolean {
  return liveNodes(state).some((node) => {
    if (node.kind !== "agent") return false;
    const toolIndex = findToolBlockIndex(node.blocks, callId);
    if (toolIndex === -1) return false;
    const tool = node.blocks[toolIndex] as ToolExecutionItem;
    if (event.type === "subagent_task") {
      return Boolean(
        tool.subagentTask?.runId === event.run_id
        && tool.subagentTask.status === event.status
        && tool.subagentTask.progress === event.progress
        && tool.subagentTask.result === event.result
        && tool.subagentTask.error === event.error,
      );
    }
    return Boolean(tool.nested && transcriptHasEvent(tool.nested, event));
  });
}

function liveNodes(state: ChatState): ChatNode[] {
  if (!state.streaming || state.liveFrom === null) return [];
  return state.nodes.slice(state.liveFrom);
}

function createAgentNode(createdAt: AgentContentEvent["created_at"]): AgentNode {
  return { kind: "agent", id: newId(), ...createTranscript(createdAt) };
}

function createTranscript(createdAt: AgentContentEvent["created_at"] | "" = ""): AgentTranscript {
  return { createdAt, agentName: "", blocks: [] };
}

function cloneAgentNode(node: AgentNode): AgentNode {
  return { ...node, ...cloneTranscript(node) };
}

function cloneTranscript(transcript: AgentTranscript): AgentTranscript {
  return { createdAt: transcript.createdAt, agentName: transcript.agentName, blocks: transcript.blocks.slice() };
}

function setAgentName(transcript: AgentTranscript, name: string) {
  if (name && !transcript.agentName) transcript.agentName = name;
}

function upsertStreamingBlock(
  blocks: TranscriptBlock[],
  kind: StreamingItem["kind"],
  segmentId: string,
  patch: { delta?: string; text?: string; complete?: boolean },
) {
  const index = findStreamingBlockIndex(blocks, kind, segmentId);
  if (index === -1) {
    const text = patch.text ?? patch.delta ?? "";
    if (hasCoveredCompletedText(blocks, kind, text)) return;
    blocks.push({ kind, id: `${kind}:${segmentId}`, segmentId, text, complete: Boolean(patch.complete) } as StreamingItem);
    return;
  }
  const existing = blocks[index] as StreamingItem;
  if (patch.delta !== undefined) {
    if (existing.complete) return;
    if (patch.delta === existing.text || existing.text.endsWith(patch.delta)) return;
    if (patch.delta.startsWith(existing.text)) {
      blocks[index] = { ...existing, text: patch.delta };
      return;
    }
  }
  if (patch.text !== undefined) {
    const duplicateIndex = findCompletedTextIndex(blocks, kind, patch.text, index);
    if (duplicateIndex !== -1) {
      blocks.splice(index, 1);
      return;
    }
    if (existing.complete && existing.text === patch.text) return;
  }
  blocks[index] = {
    ...existing,
    text: patch.text ?? existing.text + (patch.delta ?? ""),
    complete: patch.complete ?? existing.complete,
  };
}

function hasCoveredCompletedText(blocks: TranscriptBlock[], kind: StreamingItem["kind"], text: string): boolean {
  if (!text) return false;
  return blocks.some((block) => (
    block.kind === kind && block.complete && (block.text === text || block.text.startsWith(text))
  ));
}

function findCompletedTextIndex(blocks: TranscriptBlock[], kind: StreamingItem["kind"], text: string, exceptIndex = -1): number {
  if (!text) return -1;
  return blocks.findIndex((block, index) => (
    index !== exceptIndex && block.kind === kind && block.complete && block.text === text
  ));
}

function findStreamingBlockIndex(blocks: TranscriptBlock[], kind: StreamingItem["kind"], segmentId: string): number {
  for (let index = blocks.length - 1; index >= 0; index -= 1) {
    const block = blocks[index];
    if (block.kind === kind && block.segmentId === segmentId) return index;
  }
  return -1;
}

function upsertToolCall(blocks: TranscriptBlock[], callId: string, name: string, argumentsValue: Record<string, unknown>) {
  const index = findToolBlockIndex(blocks, callId, name, argumentsValue);
  if (index === -1) {
    blocks.push({ kind: "tool", id: callId || newId(), callId, name, arguments: argumentsValue, output: "", isError: false, resolved: false });
    return;
  }
  const existing = blocks[index] as ToolExecutionItem;
  blocks[index] = { ...existing, callId: existing.callId || callId, name, arguments: argumentsValue };
}

function upsertToolResult(blocks: TranscriptBlock[], callId: string, output: string, isError: boolean) {
  const index = findToolBlockIndex(blocks, callId);
  if (index === -1) {
    blocks.push({ kind: "tool", id: callId || newId(), callId, name: "", arguments: {}, output, isError, resolved: true });
    return;
  }
  const existing = blocks[index] as ToolExecutionItem;
  blocks[index] = { ...existing, output, isError, resolved: true };
}

function upsertSubagentTask(blocks: TranscriptBlock[], nextItem: SubagentExecutionItem) {
  const index = blocks.findIndex((block) => block.kind === "subagent" && block.runId === nextItem.runId);
  if (index === -1) {
    blocks.push(nextItem);
    return;
  }
  blocks[index] = nextItem;
}

function subagentExecutionItemFromEvent(event: SubagentTaskEvent): SubagentExecutionItem {
  return {
    kind: "subagent",
    id: event.run_id,
    createdAt: event.created_at,
    runId: event.run_id,
    parentAgentCode: event.parent_agent_code,
    parentAgentInstanceId: event.parent_agent_instance_id,
    agentCode: event.agent_code,
    nestedCallId: event.nested_call_id,
    status: event.status,
    result: event.result,
    error: event.error,
    progress: event.progress,
  };
}

function findToolBlockIndex(
  blocks: TranscriptBlock[],
  callId: string,
  name = "",
  argumentsValue: Record<string, unknown> | null = null,
): number {
  const byCallId = blocks.findIndex((block) => block.kind === "tool" && block.callId === callId);
  if (byCallId !== -1 || !name || argumentsValue === null) return byCallId;
  const signature = toolSignature(name, argumentsValue);
  return blocks.findIndex((block) => block.kind === "tool" && toolSignature(block.name, block.arguments) === signature);
}

function contentSignature(content: AgentInputPart[]): string {
  return content.map((part) => {
    if (part.type === "text") return `text:${part.text}`;
    return `image:${part.media_type}:${part.data.length}:${part.data.slice(0, 64)}`;
  }).join("\n");
}

function toolSignature(name: string, argumentsValue: Record<string, unknown>): string {
  return `${name}\u001f${stableJson(argumentsValue)}`;
}
