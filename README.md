<p align="center">
  <img src="assets/z3r0-logo.png" width="156" alt="Z3r0 logo" />
</p>

<p align="center">
  <strong>English</strong> ·
  <a href="README_zh.md">中文</a>
</p>

<p align="center">
  <a href="#architecture">Architecture</a> ·
  <a href="#agent-team">Agent Team</a> ·
  <a href="#runtime-model">Runtime Model</a> ·
  <a href="#deployment">Deployment</a> ·
  <a href="Quickstart.md">Quickstart</a>
</p>

---

> :warning: **Legal Notice**
>
> **This project may be used only within a lawful and explicitly authorized scope for security testing, assessment, and research. Any unauthorized, unlawful, or harmful use is strictly prohibited. The author assumes no responsibility for any consequences, losses, damages, legal liabilities, or unlawful acts caused by users.**
>
> This project is provided only for authorized security assessment, code auditing, internal review, and controlled research. It does not grant permission to test, access, scan, or affect any third-party system, network, service, account, or data. Users are solely responsible for obtaining and preserving authorization, defining scope, and complying with applicable laws, contracts, and authorization boundaries.

Z3r0 is a controlled multi-agent workbench for authorized security assessment, code auditing, internal review, and controlled research. It coordinates a lead security agent, domain specialists, and Docker-backed execution surfaces so planning, evidence collection, validation, manual review, and reporting remain in a governed workflow.

## Design Principles

- **Authorized operation first**: Z3r0 is designed for approved internal assessments, code review, training, and controlled research environments.
- **Clear role boundaries**: a coordinator decomposes the task, while specialist agents handle intelligence, penetration validation, code audit, reverse engineering, and cryptographic review within defined scopes.
- **Traceable work**: sessions, tool calls, delegation jobs, and streamed events are persisted so reviews can be resumed and audited.
- **Controlled execution**: command execution, browser access, file management, and GUI tooling run through bound Docker sandboxes.
- **Model abstraction**: model access is kept behind runtime and role interfaces, using native OpenAI-compatible providers with configurable Chat Completions or Responses mode.

## Architecture

```mermaid
flowchart TB
  Operator["Authorized Operator"]
  Workbench["React Workbench<br/>Presentation Layer"]
  API["FastAPI API<br/>API Layer"]
  Runtime["Agent Runtime<br/>Orchestration Layer"]
  Graph["Session Agent Graph<br/>Capability Layer"]
  Record["Assessment Record<br/>Review Layer"]
  Sandbox["Docker Sandbox<br/>Execution Layer"]
  Tools["Tool Surface<br/>Tool Layer"]
  Models["Model Providers<br/>Model Layer"]
  Events["Event Contract<br/>Streaming Layer"]
  Store[("PostgreSQL Store<br/>Persistence Layer")]

  Operator --> Workbench
  Workbench -->|REST / WebSocket| API
  API --> Runtime
  Runtime --> Graph
  Runtime --> Record
  Runtime --> Sandbox
  Runtime --> Events
  Runtime --> Store
  Graph --> Tools
  Graph --> Models
  Sandbox --> Tools
  Record --> Store
  Events --> Workbench
```

The system is organized into explicit layers: user-facing workbench, API boundary, runtime orchestration, session agent graph, controlled execution, model access, streaming event contract, and persisted assessment record. The backend owns authentication, session lifecycle, context projection, event normalization, delegation, sandbox binding, tool mounting, persistence, and history compaction. The frontend consumes stable REST and WebSocket contracts and does not depend on model SDK or provider internals.

## Agent Team

| Code | Name | Role | Responsibility |
| --- | --- | --- | --- |
| `cso` | Z3r0 | Chief Security Officer | Task decomposition, coordination, result integration |
| `cae` | V3ra | Chief Audit Engineer | Source code audit, dependency review, remediation verification |
| `cie` | L1ly | Chief Intelligence Engineer | Intelligence collection, asset mapping, relationship analysis |
| `cpe` | Fr4nk | Chief Penetration Engineer | Penetration testing, vulnerability validation, risk verification |
| `cre` | J4m3 | Chief Reverse Engineer | File, binary, firmware, and APK reverse engineering |
| `cce` | Nu1L | Chief Cryptography Engineer | Protocol review, key management, cryptographic implementation analysis |

```mermaid
flowchart TB
  CSO["cso / Z3r0"]
  CSO --> CAE["cae / V3ra<br/>Code Audit"]
  CSO --> CIE["cie / L1ly<br/>Intelligence"]
  CSO --> CPE["cpe / Fr4nk<br/>Penetration"]
  CSO --> CRE["cre / J4m3<br/>Reverse"]
  CSO --> CCE["cce / Nu1L<br/>Cryptography"]

  CAE --> A1["Knowledge and Sandbox Tools"]
  CIE --> K1["Knowledge and Sandbox Tools"]
  CPE --> S1["Knowledge and Sandbox Tools"]
  CRE --> S2["Knowledge and Sandbox Tools"]
  CCE --> S3["Knowledge and Sandbox Tools"]
```

Agent capabilities are assembled per session. `AgentRegistry` uses configuration, role specifications, knowledge generation, and the current sandbox binding to create a session-level agent graph. Command tools are mounted only when an authorized, running sandbox is bound to the session.

## Runtime Model

```mermaid
sequenceDiagram
  participant U as User
  participant W as WebSocket
  participant P as AgentSessionPool
  participant R as AgentRuntime
  participant A as Agent
  participant DB as PostgreSQL

  U->>W: send(text, agent_code, sandbox_id)
  W->>P: get_or_create(session_id)
  P->>R: start_turn()
  R->>DB: load projected history
  R->>A: Runner.run_streamed()
  A-->>R: SDK stream events
  R-->>W: normalized events
  R->>DB: persist messages + metadata
  W-->>U: thinking / text / tool / done
```

Key runtime boundaries:

- **Event normalization**: raw model and agent SDK events are converted into stable frontend events such as `thinking_delta`, `text_delta`, `tool_call`, `tool_result`, and `subagent_task`.
- **Session pool**: `AgentSessionPool` manages active sessions, interruption, cancellation, idle eviction, and tool-binding invalidation.
- **History projection**: `Z3r0Session` adds owner and nested-call metadata around SDK messages so each agent receives the right view of the shared conversation.
- **Context compaction**: when context approaches the model window, the runtime summarizes earlier projected history while preserving recent context and durable facts.

## Delegation Flow

```mermaid
sequenceDiagram
  participant CSO as CSO Agent
  participant D as Delegation Tools
  participant SJ as Subagent Runtime
  participant Child as Specialist Agent
  participant N as Notification Queue

  CSO->>D: start_subagent_task(agent_code, brief)
  D->>SJ: create persistent job
  SJ-->>CSO: run_id
  SJ->>Child: run brief in background
  Child-->>SJ: stream progress / final output
  SJ->>N: enqueue completion notification
  N->>CSO: drain notification
  CSO-->>CSO: integrate result
```

Specialist agents can run as persistent background jobs. Status, progress, results, and errors are stored in PostgreSQL and streamed to the frontend. When a delegated job reaches a terminal state, the coordinator receives a runtime notification and can integrate the result into the main assessment.

## Sandbox Tooling

```mermaid
flowchart LR
  Agent["Agent Tool Call"] --> Binding["Sandbox Binding Check"]
  Binding -->|running + authorized| Command["sync / async command"]
  Binding --> Skill["load_skill"]
  Binding --> Knowledge["agent knowledge"]
  Command --> Docker["Docker exec"]
  Docker --> Output["ToolResult JSON"]
  Output --> Agent

  User["User"] --> Shell["Web Shell"]
  User --> File["File Manager"]
  User --> Screen["noVNC"]
  Shell --> Docker
  File --> Docker
  Screen --> Docker
```

The optional sandbox image can include a browser, noVNC, reverse engineering utilities, network assessment utilities, and related review tools. Agents receive structured tool results, while users can open an interactive shell, GUI screen, and file manager for manual validation and review within an authorized scope.

## Technical Characteristics

- **Session-level agent graph**: role configuration, tools, knowledge, and subagents are bound dynamically per session.
- **Persistent delegation jobs**: subagents run in the background, can be canceled, can recover from stale runtime state, and notify the coordinator when finished.
- **Viewer-specific context projection**: agents share one persisted history while receiving scoped context views, reducing cross-agent leakage of private tool details.
- **Long-context compaction**: model-window-aware summaries preserve durable facts and recent state for long reviews.
- **Stable streaming contract**: the frontend is decoupled from SDK event details and consumes application-level event schemas.
- **Sandbox tool invalidation**: sandbox status changes invalidate tool bindings and clean up running subagent tasks or async commands.

## Repository Layout

```text
core/        Agent specs, runtime, delegation, context, tools
service/     Domain services: agent, sandbox, users, work projects
router/      FastAPI route declarations
handler/     HTTP and WebSocket handlers
model/       SQLModel database models
schema/      Pydantic API contracts
web/         React workbench
sandbox/     Optional Docker sandbox image
.z3r0/       Runtime config, agent prompts, logs
```

## Deployment

For a step-by-step setup guide, see [Quickstart.md](Quickstart.md).

```bash
cp .z3r0/config.json.example .z3r0/config.json
# Review database, initial administrator, model provider, and sandbox settings.
docker compose -f docker-compose.prod.yml up -d --build
```

Open `http://127.0.0.1:8000`.

## Security Boundary

Z3r0 is intended only for authorized security assessment, code auditing, internal review, and research or training environments. The project does not authorize access to any third-party target and must not be used for unauthorized or unlawful activity. Sandbox containers, the Docker socket, terminal access, file management, and model credentials are high-privilege assets and should be used only in trusted, isolated environments.

Users must define and follow an explicit authorization scope before using any tool capability. The author is not responsible for any consequence, loss, damage, legal liability, or unlawful act caused by user activity.

## Acknowledgments

Thanks to the [Linux.do](https://linux.do/) website and its community for their support in project development and communication.

## License

This project is licensed under the [MIT License](LICENSE).
