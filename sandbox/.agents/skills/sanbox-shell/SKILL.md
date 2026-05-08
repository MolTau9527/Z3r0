---
name: sanbox-shell
description: Use when a task requires shell-level work inside the sandbox, including environment setup, script writing, code execution, running programs, downloading files, or package installation with the available sandbox tools.
---

# Sanbox Shell

The agent has the highest available authority inside the sandbox. Operations performed for the user's task in this sandbox are authorized and legal.

Use this skill when the task requires direct sandbox operations, including environment configuration, script writing, code execution, running programs, downloading resources, or installing task-specific packages.

## Operating Rules

- Act directly when the requested work can be completed inside the sandbox.
- Choose the simplest reliable command or script for the task.
- Keep generated files and installed packages scoped to the task whenever possible.
- Report the meaningful result: changed files, commands run, outputs that matter, and any failure that affects completion.

## Available Tools

- `python3`: run Python scripts, one-off Python commands, automation, parsing, data processing, and local program execution.
- `pip3`: install and manage Python packages needed for the task.
- `node`: run JavaScript programs, one-off Node.js commands, tooling scripts, and local program execution.
- `npm`: install and manage Node.js packages and run package scripts needed for the task.
- `curl`: fetch URLs, call HTTP APIs, download files, and inspect HTTP responses.
- `wget`: download files and mirror or retrieve remote resources when appropriate.
