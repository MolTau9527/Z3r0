---
name: dev-browser
description: Browser automation with persistent page state. Use when users ask to navigate websites, fill forms, take screenshots, extract web data, test web apps, or automate browser workflows. Trigger phrases include "go to [url]", "click on", "fill out the form", "take a screenshot", "scrape", "automate", "test the website", "log into", or any browser interaction request.
---

# Dev Browser

A CLI for controlling browsers with sandboxed JavaScript scripts.

## Usage

Run `dev-browser --help` to learn more.

## Safety taboos

- All operations must use `--connect` to connect directly to an already running browser process; opening another browser is prohibited!
- Generally, screenshots are not required unless explicitly requested by the user.