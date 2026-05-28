"""Command output capture and read protocol for sandbox tools."""

from __future__ import annotations

import re
import shlex
from pathlib import PurePosixPath
from uuid import uuid4

from schema.sandbox.async_jobs import SandboxAsyncJobStatus
from schema.sandbox.command_outputs import (
    SandboxCommandOutputChunk,
    SandboxCommandResultMetadata,
)


OUTPUT_CHUNK_LINE_COUNT = 200
OUTPUT_DIR = "/tmp/shell-command-output"
_OUTPUT_PREFIX = f"{OUTPUT_DIR}/"
_META_PREFIX = "__z3r0_command_meta__"
_OUTPUT_FILE_RE = re.compile(
    r"^(?:[0-9a-f]{32}|[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})\.log$",
    re.IGNORECASE,
)
_TERMINAL_STATUSES = frozenset({
    SandboxAsyncJobStatus.COMPLETED,
    SandboxAsyncJobStatus.FAILED,
    SandboxAsyncJobStatus.CANCELED,
})


def new_output_path() -> str:
    return output_path_for_run(new_run_id())


def new_run_id() -> str:
    return uuid4().hex


def output_path_for_run(run_id: str) -> str:
    return f"{OUTPUT_DIR}/{run_id}.log"


def result_metadata(
    *,
    status: SandboxAsyncJobStatus,
    output_file: str | None = None,
    output_bytes: int,
    output_lines: int,
    exit_code: int | None,
    run_id: str | None = None,
    error: str | None = None,
) -> SandboxCommandResultMetadata:
    terminal = status in _TERMINAL_STATUSES
    return SandboxCommandResultMetadata(
        status=status,
        exit_code=exit_code,
        output_file=validate_output_path(output_file) if output_file else None,
        output_bytes=max(output_bytes, 0),
        output_lines=max(output_lines, 0),
        run_id=run_id if not terminal else None,
        error=error or None,
    )


def capture_command(command: str, output_path: str) -> str:
    quoted_command = shlex.quote(command)
    quoted_output_dir = shlex.quote(OUTPUT_DIR)
    quoted_output_path = shlex.quote(output_path)
    return "\n".join(
        (
            "set +e",
            f"output_dir={quoted_output_dir}",
            f"output_path={quoted_output_path}",
            'mkdir -p "$output_dir" || exit 125',
            'rm -f "$output_path"',
            ': > "$output_path" || exit 125',
            f"/bin/sh -lc {quoted_command} > \"$output_path\" 2>&1 &",
            "command_pid=$!",
            'trap \'kill -TERM "$command_pid" 2>/dev/null\' TERM INT HUP',
            'wait "$command_pid"',
            "command_exit_code=$?",
            "trap - TERM INT HUP",
            *_stat_output_lines('"$output_path"'),
            f"printf '{_META_PREFIX} %s %s\\n' \"$output_bytes\" \"$output_lines\"",
            'exit "$command_exit_code"',
        )
    )


def async_command(command: str, output_path: str) -> str:
    quoted_command = shlex.quote(command)
    quoted_output_dir = shlex.quote(OUTPUT_DIR)
    quoted_output_path = shlex.quote(output_path)
    return "\n".join(
        (
            "set +e",
            f"output_dir={quoted_output_dir}",
            f"output_path={quoted_output_path}",
            'mkdir -p "$output_dir" || exit 125',
            'rm -f "$output_path"',
            ': > "$output_path" || exit 125',
            f"/bin/sh -lc {quoted_command} > \"$output_path\" 2>&1 &",
            "command_pid=$!",
            'trap \'kill -TERM "$command_pid" 2>/dev/null\' TERM INT HUP',
            'wait "$command_pid"',
            "command_exit_code=$?",
            "trap - TERM INT HUP",
            'exit "$command_exit_code"',
        )
    )


def stat_command(output_path: str) -> str:
    quoted_output_path = shlex.quote(output_path)
    return "; ".join(
        (
            f"test -f {quoted_output_path} || exit 0",
            *_stat_output_lines(quoted_output_path),
            'printf "%s %s\\n" "$output_bytes" "$output_lines"',
        )
    )


def parse_capture_stats(raw: str) -> tuple[int, int]:
    meta_match = re.search(rf"^{re.escape(_META_PREFIX)}\s+(\d+)\s+(\d+)\s*$", raw, re.MULTILINE)
    output_bytes = int(meta_match.group(1)) if meta_match else 0
    output_lines = int(meta_match.group(2)) if meta_match else 0
    return output_bytes, output_lines


def read_command(output_file: str, start_line: int, line_count: int) -> tuple[str, int, int, int]:
    start, count, end = normalize_read_range(start_line, line_count)
    quoted_output_file = shlex.quote(validate_output_path(output_file))
    command = (
        f"test -f {quoted_output_file} "
        f"&& sed -n '{start},{end}p' {quoted_output_file} "
        "|| { printf 'command output file not found\\n' >&2; exit 1; }"
    )
    return command, start, count, end


def output_chunk(*, output_file: str, start_line: int, line_count: int, content: str) -> SandboxCommandOutputChunk:
    start, count, end = normalize_read_range(start_line, line_count)
    return SandboxCommandOutputChunk(
        output_file=validate_output_path(output_file),
        start_line=start,
        end_line=end,
        content=content,
    )


def normalize_read_range(start_line: int, line_count: int) -> tuple[int, int, int]:
    start = max(1, int(start_line))
    count = min(max(1, int(line_count)), OUTPUT_CHUNK_LINE_COUNT)
    end = start + count - 1
    return start, count, end


def validate_output_path(output_file: str) -> str:
    stripped = output_file.strip()
    normalized = str(PurePosixPath(stripped))
    parts = PurePosixPath(normalized).parts
    filename = parts[-1] if parts else ""
    if (
        not normalized.startswith(_OUTPUT_PREFIX)
        or normalized != stripped
        or parts != ("/", "tmp", "shell-command-output", filename)
        or not _OUTPUT_FILE_RE.fullmatch(filename)
    ):
        raise ValueError("output_file must be a command result path returned by sandbox command tools")
    return normalized


def _stat_output_lines(quoted_output_path: str) -> tuple[str, ...]:
    return (
        f'output_bytes=$(wc -c < {quoted_output_path} 2>/dev/null | tr -d "[:space:]")',
        f"output_lines=$(sed -n '$=' {quoted_output_path} 2>/dev/null | tr -d '[:space:]')",
        'case "$output_bytes" in ""|*[!0-9]*) output_bytes=0 ;; esac',
        'case "$output_lines" in ""|*[!0-9]*) output_lines=0 ;; esac',
    )
