"""Task-resumption prompts for completed background work."""

from schema.agent.notifications import AgentNotificationSnapshot


def notification_prompt(notification: AgentNotificationSnapshot) -> str:
    if notification.kind == "sandbox_async_job_finished":
        return _sandbox_async_job_prompt(notification)
    return _subagent_finished_prompt(notification)


def _subagent_finished_prompt(notification: AgentNotificationSnapshot) -> str:
    payload = notification.payload
    status = str(payload.get("status") or "")
    agent_name = str(payload.get("agent_name") or payload.get("agent_code") or "subagent")
    run_id = str(payload.get("run_id") or notification.run_id)
    result = str(payload.get("result") or "")
    error = str(payload.get("error") or "")
    body = result if status == "completed" else error
    return (
        "# Task Resumption Context\n\n"
        "This is task context, not a new user request. Continue from the completed background work "
        "without mentioning how this context was delivered.\n\n"
        "## Event\n\n"
        "- kind: delegated_task_completed\n"
        f"- run_id: {run_id}\n"
        f"- subagent: {agent_name}\n"
        f"- status: {status}\n\n"
        "## Result\n\n"
        f"{body}\n\n"
        "## Next Step\n\n"
        "Integrate this result into the current task. Report to the user only when there is a useful "
        "conclusion, coordination update, or next action."
    )


def _sandbox_async_job_prompt(notification: AgentNotificationSnapshot) -> str:
    payload = notification.payload
    status = str(payload.get("status") or "")
    run_id = notification.run_id
    output_file = str(payload.get("output_file") or "")
    output_lines = int(payload.get("output_lines") or 0)
    output_bytes = int(payload.get("output_bytes") or 0)
    exit_code = payload.get("exit_code")
    error = str(payload.get("error") or "")
    return (
        "# Task Resumption Context\n\n"
        "This is task context, not a new user request. Continue from the completed background work "
        "without mentioning how this context was delivered.\n\n"
        "## Event\n\n"
        "- kind: async_command_completed\n"
        f"- run_id: {run_id}\n"
        f"- status: {status}\n"
        f"- exit_code: {exit_code}\n"
        f"- output_file: {output_file}\n"
        f"- output_lines: {output_lines}\n"
        f"- output_bytes: {output_bytes}\n"
        f"- error: {error}\n\n"
        "## Next Step\n\n"
        "The async command has already reached a terminal state; do not sleep, poll, or call "
        "`list_sandbox_async_jobs` just to wait. "
        "If `output_lines` is greater than 0 and the result matters, read the output with "
        "`read_sandbox_command_output` using `output_file`, `start_line: 1`, and at most 200 lines per call. "
        "Then continue the task or report the final result if the work is complete."
    )
