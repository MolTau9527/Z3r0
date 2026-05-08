import { Button, TextArea } from "@douyinfe/semi-ui";
import { AtSign, OctagonX, Send, Square } from "lucide-react";
import { KeyboardEvent, useCallback, useEffect, useMemo, useRef, useState } from "react";
import { AgentPicker } from "./AgentPicker";
import type { AgentInfo } from "../../shared/api/types";

type ComposerProps = {
  streaming: boolean;
  disabled?: boolean;
  agents: AgentInfo[];
  activeAgentCode: string;
  agentSwitchDisabled?: boolean;
  canCancelAll?: boolean;
  onPickAgent: (code: string) => void;
  onSend: (text: string) => void;
  onInterrupt: () => void;
  onCancelAll: () => void;
};

export function Composer({
  streaming,
  disabled = false,
  agents,
  activeAgentCode,
  agentSwitchDisabled = false,
  canCancelAll = false,
  onPickAgent,
  onSend,
  onInterrupt,
  onCancelAll,
}: ComposerProps) {
  const [text, setText] = useState("");
  const [highlight, setHighlight] = useState(0);
  const [pickerOpen, setPickerOpen] = useState(false);

  const wrapperRef = useRef<HTMLDivElement>(null);

  const activeAgent = useMemo(
    () => agents.find((agent) => agent.code === activeAgentCode) ?? null,
    [agents, activeAgentCode],
  );

  useEffect(() => {
    if (!pickerOpen) return;
    if (highlight >= agents.length) {
      setHighlight(Math.max(0, agents.length - 1));
    }
  }, [agents.length, highlight, pickerOpen]);

  const closePicker = useCallback(() => {
    setPickerOpen(false);
    setHighlight(0);
  }, []);

  useEffect(() => {
    if (!pickerOpen) return;
    const handler = (event: MouseEvent) => {
      const target = event.target as Node | null;
      if (target && wrapperRef.current?.contains(target)) return;
      closePicker();
    };
    document.addEventListener("mousedown", handler);
    return () => document.removeEventListener("mousedown", handler);
  }, [closePicker, pickerOpen]);

  const focusTextarea = useCallback(() => {
    wrapperRef.current?.querySelector("textarea")?.focus();
  }, []);

  const submit = () => {
    const trimmed = text.trim();
    if (!trimmed || streaming || disabled) return;
    onSend(trimmed);
    setText("");
    closePicker();
  };

  const pickAgent = (agent: AgentInfo) => {
    if (agentSwitchDisabled) return;
    onPickAgent(agent.code);
    closePicker();
    focusTextarea();
  };

  const toggleChip = () => {
    if (agentSwitchDisabled) return;
    setPickerOpen((next) => !next);
    focusTextarea();
  };

  const agentSwitchDisabledReason = "Finish or cancel running subagent tasks before switching agents";

  const handleKeyDown = (event: KeyboardEvent<HTMLTextAreaElement>) => {
    if (pickerOpen) {
      if (event.key === "Escape") {
        event.preventDefault();
        closePicker();
        return;
      }
      if (event.key === "ArrowDown" && agents.length > 0) {
        event.preventDefault();
        setHighlight((index) => (index + 1) % agents.length);
        return;
      }
      if (event.key === "ArrowUp" && agents.length > 0) {
        event.preventDefault();
        setHighlight((index) => (index - 1 + agents.length) % agents.length);
        return;
      }
      if (event.key === "Enter" && !event.shiftKey) {
        event.preventDefault();
        if (agents.length > 0 && !agentSwitchDisabled) pickAgent(agents[highlight]);
        return;
      }
      if (event.key === "Tab") {
        event.preventDefault();
        if (agents.length > 0 && !agentSwitchDisabled) pickAgent(agents[highlight]);
        return;
      }
    }

    if (event.key !== "Enter" || event.shiftKey) return;
    event.preventDefault();
    if (streaming) {
      onInterrupt();
    } else {
      submit();
    }
  };

  const action = streaming
    ? { icon: <Square size={16} />, type: "danger" as const, label: "Stop", onClick: onInterrupt, disabled: false }
    : { icon: <Send size={16} />, type: "primary" as const, label: "Send", onClick: submit, disabled: disabled || !text.trim() };

  return (
    <div ref={wrapperRef} className={`composer${streaming ? " composer-streaming" : ""}`}>
      <div className="composer-input">
        {pickerOpen ? (
          <div className="composer-picker">
            <AgentPicker
              agents={agents}
              highlightedIndex={highlight}
              disabled={agentSwitchDisabled}
              disabledReason={agentSwitchDisabledReason}
              onHover={setHighlight}
              onSelect={pickAgent}
            />
          </div>
        ) : null}
        <div className="composer-row">
          <button
            type="button"
            className="composer-agent-chip"
            onClick={toggleChip}
            disabled={agentSwitchDisabled}
            aria-label={activeAgent ? `Speaking to ${activeAgent.name}` : "Pick an agent"}
            title={agentSwitchDisabled ? agentSwitchDisabledReason : activeAgent ? "Click to switch agent" : "Pick an agent"}
          >
            <AtSign size={14} />
            <span>{activeAgent?.name || "Agent"}</span>
          </button>
          <TextArea
            value={text}
            onChange={setText}
            onKeyDown={handleKeyDown}
            autosize={{ minRows: 1, maxRows: 8 }}
            disabled={disabled && !streaming}
            placeholder={
              disabled
                ? "Loading conversation history…"
                : streaming
                  ? "Streaming response… press Enter or stop to interrupt"
                  : "Send a message · Shift+Enter for newline"
            }
          />
          <Button
            icon={action.icon}
            theme="solid"
            type={action.type}
            onClick={action.onClick}
            disabled={action.disabled}
            aria-label={streaming ? "Interrupt streaming" : "Send message"}
          >
            {action.label}
          </Button>
          <Button
            icon={<OctagonX size={16} />}
            theme="solid"
            type="danger"
            onClick={onCancelAll}
            disabled={disabled || !canCancelAll}
            aria-label="Cancel all running subagent tasks"
            title={canCancelAll ? "Cancel all running subagent tasks" : "No running subagent tasks"}
          />
        </div>
      </div>
    </div>
  );
}
