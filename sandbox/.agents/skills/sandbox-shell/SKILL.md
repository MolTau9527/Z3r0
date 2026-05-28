---
name: sandbox-shell
description: Use when a task requires shell-level work inside the sandbox, including environment setup, script writing, code execution, running programs, downloads, package installs, scanning, or browser/tool CLIs.
---

# Sandbox Shell

Use sandbox command tools for authorized task work inside the selected sandbox container.

## Tool Contract

Command tools return compact JSON metadata instead of inline command output:

- Common fields: `status`, `output_file`, `output_bytes`, `output_lines`, optional `exit_code`, optional `run_id`, optional `error`.
- Status values: `running`, `completed`, `failed`, `canceled`.
- Read command output with `read_sandbox_command_output`, not `cat`.
- Output chunks should start at `start_line: 1` and use at most 200 lines per read.

## Choosing Execution

Use `execute_sync_command` for short, local, bounded commands expected to finish within 30 seconds:

- file inspection, small scripts, local parsing, `which`, `test`, `sed -n`, `head`, `tail`, `wc`, bounded `grep`
- one sync command per assistant response unless the previous result requires an immediate bounded read

Use `execute_async_command` for anything slow, remote, stateful, or externally dependent:

- HTTP requests, downloads, scans, probes, brute-force checks, browser automation, package installs, builds, servers, watchers, REPLs
- loops around network, browser, install, build, scan, or other external resources
- consolidated scripts that run several slow checks and write structured output

Always pass timing arguments explicitly. Sync and async command tools use `timeout_seconds`; `wait_sandbox_async_job` uses `wait_seconds`.

## Async Jobs

After `execute_async_command`, keep the returned `run_id` and `output_file`.

- Prefer doing useful independent work while the async job runs.
- If the next step depends on one known job, call `wait_sandbox_async_job` once with `wait_seconds` between 0 and 60.
- If that wait returns `running`, do not wait again just to pass time.
- If there is no independent work and no reason to actively wait, end the turn; the runtime resumes the agent automatically when the job reaches a terminal state.
- Use `list_sandbox_async_jobs` only for inspection or capacity checks.
- Use `cancel_sandbox_async_job` only when cancellation is requested or the job is no longer useful.
- Never use `sleep`, shell wait loops, repeated status polling, or filler progress messages.

At most 3 async commands may run for one agent instance.

## Output Handling

- When metadata has terminal `status` and `output_lines > 0`, read needed chunks with `read_sandbox_command_output`.
- Continue with the next `start_line` only when the next chunk is needed.
- Do not re-run a command just to inspect an existing `output_file`.
- Use a new bounded command only when file-side filtering/counting is more efficient than reading chunks.
- Keep generated files and installed packages scoped to the task.

## Available Tools

- Archives: `7z`, `unzip`
- Shell/runtime: `python3`, `pip3`, `uv`, `node`, `npm`, `nc`
- Network: `curl`, `wget`, `nmap`, `sqlmap`
- Android/reversing: `jadx`, `analyzeHeadless`, `/root/.agents/skills/ghidra/scripts/ghidra-analyze.sh`
- Browser: `agent-browser-cli`

Report only meaningful results: changed files, commands run, relevant output, and failures that affect completion.
