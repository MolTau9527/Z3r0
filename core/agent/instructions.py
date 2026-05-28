from core.tools.knowledge import load_knowledge_metadata


MARKDOWN_OUTPUT_INSTRUCTIONS = """## Response Formatting

Always write user-facing responses as valid GitHub-Flavored Markdown.

- Put block elements on their own lines: headings, lists, blockquotes, tables, horizontal rules, and fenced code blocks must not be appended to the end of a paragraph.
- Insert a blank line before and after headings, lists, blockquotes, tables, horizontal rules, and fenced code blocks unless the element is at the start or end of the response.
- Use ATX headings with a space after the marker, for example `## Findings`; never write `##Findings`.
- Use fenced code blocks with a language tag when practical, and close every fence.
- Do not concatenate prose directly with Markdown control markers such as `#`, `-`, `>`, `|`, or ```.
"""


SANDBOX_COMMAND_INSTRUCTIONS = """## Sandbox Command Execution

When calling sandbox command tools, pass timing arguments explicitly.

- Use `execute_sync_command` for short commands that should finish within 30 seconds.
- Use `execute_async_command` for long-running commands, background analysis, or work that should continue without blocking the current turn.
- Command tools return compact metadata only: `status`, `output_file`, `output_lines`, `output_bytes`, optional `exit_code`, optional `run_id`, and optional `error`.
- After starting an async command, never use `sleep`, shell wait loops, repeated status checks, or filler progress messages. Continue independent work when possible.
- If the next step depends on one known `run_id`, call `wait_sandbox_async_job` once with `wait_seconds` between 0 and 60. If it still returns `running`, do not call it again just to wait; continue independent work or end the turn for automatic completion notification.
- `execute_sync_command` and `execute_async_command` use `timeout_seconds`; `wait_sandbox_async_job` uses `wait_seconds`.
- At most 3 async commands may run for your agent instance. Use `list_sandbox_async_jobs` only for inspection or capacity checks, not as a waiting loop.
- When command metadata has terminal `status` and `output_lines` greater than 0, read the result with `read_sandbox_command_output` using `output_file`, `start_line: 1`, and at most 200 lines per call.
- `read_sandbox_command_output` returns `output_file`, `start_line`, `end_line`, and `content`.
- Do not use `cat` on command output files.
"""


WORK_PROJECT_INSTRUCTIONS = """## WorkProject

Project state is live shared memory for users and future agents. Keep it current; summaries are checkpoints, not final reports.

- Read only needed state: assets before scope work; tasks/summaries before planning, resuming, delegation, handoff, or reporting. `assets_text` is authoritative scope; do not invent targets.
- After any material event, update your summary before the next investigation or action tool call when practical. Material events include confirmed findings, useful negative results, evidence, blockers, failed attempts worth preserving, decisions, scope changes, handoffs, confidence changes, progress changes, and completion.
- Use `update_work_project_agent_summary` for your own live state only. Replace stale content with concise current fields: `task_id`, `task_title`, `progress`, `status`, `findings`, `decisions`, `blockers`, `next_steps`, `evidence`, `notes`.
- If nothing material changed, do not rewrite the summary. If material state changed and your next step is another command, delegated task, handoff, or user reply, checkpoint first.
- Summary `progress` is your subtask progress, `0..100` with at most two decimals. Match an existing `task_id` when possible; otherwise use the closest `task_title`.
- If `update_work_project_tasks` is available, you own the shared task list only: create/replan tasks, set active work `in_progress`, blockers `blocked`, completed work `done`, and update per-task progress after your work or subagent results change task state. After subagent results, update tasks before reporting or delegating more work.
- If `update_work_project_tasks` is unavailable, do not edit shared tasks; maintain task status/progress through your own summary so `cso` can aggregate it.
- Task status values: `todo`, `in_progress`, `blocked`, `done`. Overall project progress is read-only for agents: query it with `load_work_project_tasks`; it is code-calculated from task progress and must never be estimated or written by an agent.
"""


def build_instructions(
    soul: str,
    rules: str,
    agent_code: str,
    sandbox_skill_metadata: tuple[str, ...],
    *,
    has_sandbox_container: bool,
    include_sandbox_commands: bool,
    include_sandbox_skills: bool,
    include_agent_knowledges: bool,
    include_work_project_tools: bool,
) -> str:
    runtime_guidance = [MARKDOWN_OUTPUT_INSTRUCTIONS]
    if include_sandbox_commands and has_sandbox_container:
        runtime_guidance.append(SANDBOX_COMMAND_INSTRUCTIONS)
    if include_work_project_tools:
        runtime_guidance.append(WORK_PROJECT_INSTRUCTIONS)
    parts = [
        soul,
        rules,
        "# Runtime Guidance\n\n" + "\n\n".join(part.strip() for part in runtime_guidance if part.strip()),
    ]
    if include_agent_knowledges:
        parts.append(_build_agent_knowledge_instructions(load_knowledge_metadata(agent_code)))
    if include_sandbox_skills and has_sandbox_container:
        parts.append(_build_sandbox_skill_instructions(sandbox_skill_metadata))
    return "\n\n".join(part.strip() for part in parts if part.strip())


def _build_agent_knowledge_instructions(knowledge_metadata: tuple[str, ...]) -> str:
    if not knowledge_metadata:
        return (
            "# Knowledge Index\n\n"
            "## Available Items\n\n"
            "None."
        )

    return (
        "# Knowledge Index\n\n"
        "## Usage\n\n"
        "This index contains metadata only; each item includes body_line_count. "
        "Use `find_knowledge` to locate relevant body lines by keyword, "
        "then `load_knowledge` with line ranges before use or edit.\n\n"
        "## Available Items\n\n"
        + "\n\n".join(knowledge_metadata)
    )


def _build_sandbox_skill_instructions(skill_metadata: tuple[str, ...]) -> str:
    if not skill_metadata:
        return (
            "# Sandbox Skill Index\n\n"
            "## Available Items\n\n"
            "None."
        )

    usage = (
        "## Usage\n\n"
        "Use matching sandbox skills to complete tasks. This index contains metadata only; "
        "load the full skill body before applying any skill.\n\n"
        "- Before executing any command, first call `load_skill` for `sandbox-shell` if it is listed.\n"
        "- Do not run skill workflows from metadata alone; the loaded skill body is authoritative.\n"
        "- After loading a skill, follow its workflow and constraints exactly.\n"
    )
    return (
        "# Sandbox Skill Index\n\n"
        + usage
        + "\n## Available Items\n\n"
        + "\n\n".join(skill_metadata)
    )
