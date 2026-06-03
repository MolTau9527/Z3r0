# Z3r0 Quickstart

<p>
  <strong>English</strong> ·
  <a href="QUICKSTART_zh.md">中文</a> ·
  <a href="README.md">README</a>
</p>

This guide starts Z3r0 with Docker Compose for an authorized, controlled environment. Review the configuration before exposing the service to a network.

## Prerequisites

- Docker Engine with Docker Compose
- Access to the Docker socket on the host that runs Z3r0
- A model provider endpoint compatible with the configured agent models

## 1. Clone the repository

```bash
git clone https://github.com/yv1ing/Z3r0.git
cd Z3r0
```

## 2. Create the runtime configuration

```bash
cp .z3r0/config.json.example .z3r0/config.json
```

Edit `.z3r0/config.json` before starting the service:

- Set `system.encrypt_key` to a random value with at least 32 characters.
- Set a strong `system.bootstrap_admin.password`.
- Review `database` settings. The production Compose file starts PostgreSQL with the defaults shown in the example.
- Configure each agent under `agents` with the intended `base_url`, `api_key`, `model`, and `context_window`.

## 3. Start Z3r0

```bash
docker compose -f docker-compose.prod.yml up -d --build
```

The application listens on:

```text
http://127.0.0.1:8000
```

Sign in with the bootstrap administrator configured in `.z3r0/config.json`.

## 4. Verify the deployment

```bash
docker compose -f docker-compose.prod.yml ps
docker compose -f docker-compose.prod.yml logs -f app
```

The backend creates database tables during startup and serves the built React workbench from the same application container.

## Operational Notes

- Z3r0 is intended for authorized security testing, red team exercises, code auditing, research, and training environments.
- The application container mounts `/var/run/docker.sock` so it can manage sandbox containers. Treat the host as trusted and isolated.
- Model credentials, terminal access, file management, and sandbox containers are high-privilege assets.
- Change the PostgreSQL and pgAdmin defaults in `docker-compose.prod.yml` before deploying outside a local evaluation environment.
