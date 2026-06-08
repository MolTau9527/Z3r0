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
  <a href="QUICKSTART.md">Quickstart</a>
</p>

---

> :warning: **Legal Notice**
>
> **This project may be used only within a lawful and explicitly authorized scope for security testing, assessment, and research. Any unauthorized, unlawful, or harmful use is strictly prohibited. The author assumes no responsibility for any consequences, losses, damages, legal liabilities, or unlawful acts caused by users.**
>
> This project is provided only for authorized security assessment, code auditing, internal review, and controlled research. It does not grant permission to test, access, scan, or affect any third-party system, network, service, account, or data. Users are solely responsible for obtaining and preserving authorization, defining scope, and complying with applicable laws, contracts, and authorization boundaries.

Z3r0 is a controlled multi-agent workbench for authorized security assessment, code auditing, internal review, and controlled research. It coordinates a lead security agent, domain specialists, Docker-backed execution surfaces, and WorkProject records so planning, asset discovery, risk validation, relationship mapping, attack-path reconstruction, and manual review remain in one governed workflow.

## Design Principles

- **Authorized operation first**: Z3r0 is designed for approved internal assessments, code review, training, and controlled research environments.
- **Clear role boundaries**: a coordinator decomposes the task, while specialist agents handle intelligence, penetration validation, code audit, reverse engineering, and cryptographic review within defined scopes.
- **Traceable work**: sessions, tool calls, delegation jobs, and streamed events are persisted so reviews can be resumed and audited.
- **Durable project records**: WorkProject sessions persist assets, findings, relationship edges, and attack paths as first-class review objects.
- **Controlled execution**: command execution, browser access, file management, and GUI tooling run through bound Docker sandboxes.
- **Model abstraction**: model access is kept behind runtime and role interfaces, using native OpenAI-compatible providers with configurable Chat Completions or Responses mode.

## Architecture

```mermaid
flowchart TB
  Operator["Authorized Operator"]
  Workbench["React Workbench<br/>Presentation Layer"]
  API["FastAPI API<br/>API Layer"]
  Runtime["Agent Runtime<br/>Orchestration Layer"]
  Drivers["Instance Drivers<br/>Async Scheduling Layer"]
  Notifications["Notification Obligations<br/>Liveness Layer"]
  Graph["Session Agent Graph<br/>Capability Layer"]
  Timeline["Timeline Event Log<br/>Replay Layer"]
  Record["WorkProject Records<br/>Review Layer"]
  Sandbox["Docker Sandbox<br/>Execution Layer"]
  Tools["Tool Surface<br/>Tool Layer"]
  Models["Model Providers<br/>Model Layer"]
  Events["Event Contract<br/>Streaming Layer"]
  Store[("PostgreSQL Store<br/>Persistence Layer")]

  Operator --> Workbench
  Workbench -->|REST / WebSocket| API
  API --> Runtime
  Runtime --> Drivers
  Runtime --> Graph
  Runtime --> Record
  Runtime --> Sandbox
  Runtime --> Events
  Runtime --> Store
  Drivers --> Notifications
  Notifications --> Runtime
  Events --> Timeline
  Timeline --> Store
  Graph --> Tools
  Graph --> Models
  Sandbox --> Tools
  Record --> Store
  Events --> Workbench
```

The system is organized into explicit layers: user-facing workbench, API boundary, runtime orchestration, resumable instance drivers, notification-backed liveness, session agent graph, controlled execution, model access, streaming event contract, durable timeline replay, and persisted WorkProject records. The backend owns authentication, session lifecycle, context projection, event normalization, delegation, sandbox binding, tool mounting, notification obligations, persistence, project-scoped records, and history compaction. The frontend consumes stable REST and WebSocket contracts and does not depend on model SDK or provider internals.

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

Agent capabilities are assembled per session. `AgentRegistry` uses configuration, role specifications, knowledge generation, the current sandbox binding, and the current WorkProject binding to create a session-level agent graph. Command tools are mounted only when an authorized, running sandbox is bound to the session. WorkProject record tools are mounted only for project sessions, keeping ordinary chat sessions separate from assets, findings, relationship edges, and attack paths.

## Runtime Model

```mermaid
sequenceDiagram
  participant U as User
  participant W as WebSocket
  participant P as AgentSessionPool
  participant S as AgentSession
  participant TR as TaskRuntime
  participant A as Agent
  participant N as Notifications
  participant T as Timeline
  participant DB as PostgreSQL

  U->>W: send(text, agent_code, sandbox_id)
  W->>P: get_or_create(session_id)
  P->>S: start_turn(content)
  S->>S: launch main instance driver
  S->>TR: run_until_idle(initial_content)
  TR->>DB: load projected history
  TR->>A: Runner.run_streamed()
  A-->>TR: iter_interruptible_events()
  TR-->>S: normalized events
  S-->>W: publish to subscribers
  S->>T: stamp seq + upsert persistable event
  T->>DB: timeline event log
  TR->>DB: persist messages + metadata
  W-->>U: thinking / text / tool / done

  Note over TR,A: Notification arrives during turn
  TR->>TR: InterruptSignal (deferred if tool pending)
  TR->>DB: flush_partial_context
  TR->>N: claim PENDING notification
  N-->>TR: notification prompt / user message
  TR->>TR: run notification turn
  S->>S: stop when no PENDING work and leave AWAITING work dormant
```

Key runtime boundaries:

- **Non-blocking instance drivers**: `AgentSession._drive` and `_SubagentDriver` run the optional initial turn, drain currently claimable notifications, then settle. Drivers stop while background work is still `AWAITING`; completion notifications relaunch the owning main or subagent instance when integration work is ready.
- **Interrupt-driven task execution**: `run_until_idle` manages the agent turn lifecycle; `iter_interruptible_events` races the SDK event stream against notification signals and raises `InterruptSignal` at safe points (after pending tool calls complete), modeled after CPU interrupt masking for atomicity.
- **Notification-backed liveness**: `AgentNotification` rows are the single source of truth for active work. `AWAITING` tracks running background obligations, `PENDING` wakes the owning agent, and `PROCESSING` marks a claimed notification turn.
- **Turn-terminal async commands**: `execute_async_command` dispatches a sandbox command, returns only `status` and `run_id`, and `AgentRegistry` ends the turn immediately via `tool_use_behavior`. The agent is resumed automatically when the command completes; there is no polling or list-wait loop.
- **Timeline event log**: live events are stamped with stable `seq` values and item keys in `TimelineLogWriter`; persistable events are upserted into the durable event log so replay reads the same wire events instead of reconstructing UI state from SDK messages.
- **Event normalization**: raw model and agent SDK events are converted into stable frontend events such as `thinking_delta`, `text_delta`, `tool_call`, `tool_result`, and `subagent_task`.
- **Session pool**: `AgentSessionPool` manages active sessions, notification recovery, interruption, cancellation, idle eviction, and tool-binding invalidation.
- **History projection**: `Z3r0Session` adds owner and nested-call metadata around SDK messages so each agent receives the right view of the shared conversation.
- **Context compaction**: when context approaches the model window, the runtime summarizes earlier projected history while preserving recent context and durable facts.

## Delegation Flow

```mermaid
sequenceDiagram
  participant CSO as CSO Agent
  participant D as Delegation Tools
  participant DB as PostgreSQL
  participant SJ as Subagent Driver
  participant Child as Specialist Agent
  participant N as Notifications
  participant P as Parent Driver

  CSO->>D: start_subagent_task(agent_code, brief)
  D->>DB: create task + AWAITING parent obligation
  D->>SJ: register _SubagentDriver and spawn drive
  SJ-->>CSO: run_id (CSO ends turn)
  SJ->>Child: run_until_idle(brief)
  Child-->>SJ: stream progress / final output
  alt child starts nested work
    SJ->>N: sees outstanding target obligations
    SJ->>SJ: go dormant with no live task
  else child reaches terminal status
    SJ->>DB: complete / fail task
    DB->>N: AWAITING -> PENDING parent obligation
    N->>P: resume_target_instance(parent)
    P->>CSO: claim result notification
    CSO-->>CSO: integrate result
  end
```

Specialist agents run through resumable per-run `_SubagentDriver` instances. Starting a subagent creates the `AgentSubordinateTask` record and the parent `SUBAGENT_FINISHED` notification obligation in one database transaction, so the parent never observes a gap where the child is neither running nor pending integration. Each subagent driver uses the same `run_until_idle` executor as the main agent, streams nested events through the session event bus, and then settles into one of three states: relaunch if a claimable notification arrived during drain, go dormant if child work or async jobs are still outstanding, or complete/fail/cancel the task.

When a subagent completes or fails, the task update and parent obligation transition (`AWAITING` -> `PENDING`) commit together. `resume_target_instance` wakes the owning driver: main-agent targets route through `AgentSessionPool.resume_session`, while subagent targets relaunch their dormant `_SubagentDriver`. Canceled subagents resolve their obligation without waking the parent.

## Sandbox Tooling

```mermaid
flowchart LR
  Agent["Agent Tool Call"] --> Binding["Sandbox Binding Check"]
  Binding -->|running + authorized| Sync["execute_sync_command"]
  Binding -->|running + authorized| Async["execute_async_command"]
  Binding --> Skill["load_skill"]
  Binding --> Knowledge["agent knowledge"]
  Sync --> Docker["Docker exec"]
  Docker --> Output["ToolResult JSON + output_file"]
  Output --> Agent
  Async --> Job[("SandboxAsyncJob<br/>AWAITING obligation")]
  Job -->|completed / failed| Notify["PENDING owner notification"]
  Notify --> Agent
  Agent --> Read["read_sandbox_command_output"]
  Read --> Docker

  User["User"] --> Shell["Web Shell"]
  User --> File["File Manager"]
  User --> Screen["noVNC"]
  Shell --> Docker
  File --> Docker
  Screen --> Docker
```

The optional sandbox image can include a browser, noVNC, reverse engineering utilities, network assessment utilities, and related review tools. Synchronous commands return captured output metadata immediately. Asynchronous commands are deliberately turn-terminal: after dispatch, the agent stops and is resumed only after the job completes or fails, with terminal status, exit code, output size, and output file delivered through the owner notification. Agents read completed output with `read_sandbox_command_output`; they do not poll running jobs.

## WorkProject Records

WorkProject sessions are the durable assessment workspace. They keep structured records outside the model context and outside SDK-owned tables:

- **Assets**: the only graph nodes. `type` is one of `service`, `domain`, `network`, or `binary`; `service`/`domain`/`network` use the `host` field (port optional for `service`), `binary` uses `path`, and a short recon `banner` is stored in the small `extra` object. `origin` marks each asset as declared `scope` or agent-`discovered`. Each asset is keyed by a normalized `(type, identifier)` identity.
- **Findings**: suspected, validated, or false-positive risks. A finding records the affected asset and carries its own proof in `description`/`impact`; when it substantiates a relationship or attack step it is attached to the relevant graph edge.
- **Relationship graph**: directed edges between two assets. The `type` is either structural (`related`, `resolves_to`, `hosts`, `connects_to`, `trusts`) describing the target architecture, or offensive (`exploits`, `pivots_to`, `leads_to`) describing attack progression. Findings attached to an edge are its supporting evidence.
- **Attack paths**: ordered chains where each step traverses one relationship edge, explaining how access or impact progressed.

These records are read through WorkProject-scoped REST APIs and project-session UI views, and are created and updated by agents through session tools when the session has a bound WorkProject; ordinary chat sessions do not receive these tools or UI entry points. Agent summaries remain compact checkpoints, while durable facts live in the structured project records. Report generation remains a planned roadmap phase and is not part of the current implementation.

### Auditable Attack Chain

The four record types form a single graph: assets are nodes, edges are directed relationships between them, findings are the evidence attached to a node and/or an edge, and an attack path is an ordered walk over edges. An edge's structural-vs-offensive category is derived from its `type` (it is not a stored column). Because every claim is pinned to the graph element it describes, the whole assessment is traceable end to end.

```mermaid
erDiagram
  ASSET {
    enum   type        "service | domain | network | binary"
    enum   origin      "scope | discovered"
    string identifier  "(type, identifier) identity"
    string created_by_agent_code  "provenance"
    string created_from_session_id "provenance"
  }
  EDGE {
    enum   type   "related|resolves_to|hosts|connects_to|trusts|exploits|pivots_to|leads_to"
    string label
    int    source_asset_id
    int    target_asset_id
  }
  FINDING {
    enum     status      "suspected | validated | false_positive"
    int      asset_id    "affected node"
    int      edge_id     "substantiated relation"
    datetime validated_at
  }
  ATTACK_PATH {
    enum   status  "suspected | validated | blocked | closed"
    string title
  }
  ATTACK_PATH_STEP {
    int sequence "ordered hop"
    int edge_id  "traversed relation"
  }

  ASSET            ||--o{ EDGE             : "source / target node"
  ASSET            ||--o{ FINDING          : "affected asset"
  EDGE             ||--o{ FINDING          : "evidence (edge_id)"
  EDGE             ||--o{ ATTACK_PATH_STEP : "traversed by"
  ATTACK_PATH      ||--o{ ATTACK_PATH_STEP : "ordered steps"
```

The chain is auditable and traceable on five axes:

- **Provenance** — every asset, edge, finding, path, and step carries `created_by_agent_code`, `created_from_session_id`, and `created_at`/`updated_at`, so each fact traces back to the exact agent and session that produced it and when.
- **Evidence binding** — a finding's `edge_id` ties proof to a specific relationship and its `asset_id` ties proof to a specific node; the proof itself (`description`/`impact`) lives in the finding, so any relation or attack step can be drilled down to the evidence that justifies it.
- **Confidence lifecycle** — a finding's `status` (`suspected` → `validated`/`false_positive`, with the moment of validation stamped by `validated_at`) and an attack path's `status` (`suspected` → `validated`, or `blocked`/`closed`) make the maturity of every claim explicit; nothing is presented as fact until it is validated.
- **Replayable path** — an attack path is an ordered list of steps, each pinned to one edge between two assets, so the route from entry to impact can be reconstructed hop by hop, with each hop carrying its own supporting findings.
- **Scope accountability & integrity** — `origin` separates declared `scope` from agent-`discovered` surface so work can be checked against the engagement boundary, and referential rules keep the graph consistent (deleting an asset purges its edges and detaches its findings; deleting an edge removes the steps that traverse it and detaches its findings), so the audit trail never holds dangling references.

## Technical Characteristics

- **True async instance drivers**: main and subagent drivers drain ready turns and then stop; they do not block on background children or long sandbox commands. Completion notifications relaunch the owning instance when integration work is ready.
- **Interrupt-driven task runtime**: `run_until_idle` provides a unified execution loop for both main and sub-agents; `iter_interruptible_events` races the SDK event stream against notification signals, raising `InterruptSignal` with CPU-interrupt-style atomicity that defers preemption until pending tool calls complete.
- **Notification obligation scheduler**: subagent tasks and sandbox async jobs register `AWAITING` obligations atomically with their own records; terminal updates flip obligations to `PENDING`, `COMPLETED`, `FAILED`, or `CANCELED` so session liveness comes from one table.
- **Turn-terminal async command dispatch**: successful `execute_async_command` calls end the agent turn immediately through SDK tool-use behavior, preventing follow-up polling and making completion notification the only resume path.
- **Session-level agent graph**: role configuration, tools, knowledge, and subagents are bound dynamically per session.
- **Self-healing delegation drivers**: subagents can be canceled while live or dormant, stale running tasks are failed on backend restart, and relaunch budgets prevent hot loops when a driver cannot make progress.
- **Durable timeline replay**: the UI timeline persists stable event payloads with monotonic `seq` values and item keys, so refresh/replay uses the same event contract as live streaming.
- **Viewer-specific context projection**: agents share one persisted history while receiving scoped context views, reducing cross-agent leakage of private tool details.
- **Long-context compaction**: model-window-aware summaries preserve durable facts and recent state for long reviews.
- **Stable streaming contract**: the frontend is decoupled from SDK event details and consumes application-level event schemas.
- **Sandbox tool invalidation**: sandbox status changes invalidate tool bindings and clean up running subagent tasks or async commands.
- **Project-scoped security records**: assets, findings, relationship edges, and attack paths are persisted as app-owned WorkProject records and replayed from stable API contracts.

## Repository Layout

```text
core/        Agent specs, runtime, task runtime, delegation, context, tools
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

For a step-by-step setup guide, see [QUICKSTART.md](QUICKSTART.md).

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
