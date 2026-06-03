# Changelog

All notable changes to Z3r0 are documented in this file.

## [0.1.0] - 2026-06-03

### Added

- Initial public preview release of Z3r0.
- Multi-agent security workbench with coordinator and specialist agents for code audit, intelligence, penetration validation, reverse engineering, and cryptography review.
- FastAPI backend with authentication, system configuration, work projects, sandbox image/container management, and agent session APIs.
- React workbench with playground chat, session replay, subagent progress, sandbox selection, file manager, shell access, and admin pages.
- Docker-backed sandbox execution for controlled command execution, async jobs, browser/noVNC workflows, and file access.
- Interrupt-driven agent runtime with resumable turns, async command completion notifications, subagent delegation, and context compaction.
- Persistent timeline event log for live streaming and replay.
- Docker Compose production quickstart with PostgreSQL and bundled frontend serving.

### Security

- Added explicit authorized-use and legal boundary documentation.
- Runtime configuration uses local `.z3r0/config.json`, with `.z3r0/config.json.example` as the tracked template.
- Sandbox and Docker socket access are documented as high-privilege deployment surfaces.

### Known Notes

- `docker-compose.prod.yml` includes default PostgreSQL and pgAdmin credentials for local evaluation; change them before exposing the service.
- The application container mounts `/var/run/docker.sock`; deploy only on trusted, isolated hosts.