import {
  Activity,
  AlertTriangle,
  Bot,
  Box,
  Braces,
  CheckCircle2,
  ClipboardCheck,
  Code2,
  Database,
  FileCheck2,
  FileSearch,
  Fingerprint,
  GitBranch,
  Layers3,
  LockKeyhole,
  MessageSquareCode,
  Network,
  ShieldCheck,
  SquareTerminal,
  Workflow,
  Zap,
  type LucideIcon,
} from "lucide-react";
import { useState } from "react";

const repositoryUrl = "https://github.com/yv1ing/Z3r0";

type LandingPrimaryAction = {
  label: string;
  href?: string;
  external?: boolean;
  onSelect?: () => void;
};

type LandingContentProps = {
  logoSrc: string;
  primaryAction: LandingPrimaryAction;
};

type ArchitectureNode = {
  id: string;
  label: string;
  role: string;
  detail: string;
  points: string[];
  icon: LucideIcon;
};

const architectureNodes: ArchitectureNode[] = [
  {
    id: "operator",
    label: "Authorized Operator",
    role: "Authorized Entry",
    detail: "The operator defines the assessment objective, authorization boundary, sandbox context, and review expectations.",
    points: [
      "Starts authorized assessment, audit, validation, or research work from the browser.",
      "Reviews streamed reasoning, evidence, tool output, and final assessment records.",
      "Can manually review shell, screen, and files when evidence needs human verification.",
    ],
    icon: Fingerprint,
  },
  {
    id: "workbench",
    label: "React Workbench",
    role: "Presentation Layer",
    detail: "The workbench is the user-facing surface for sessions, resource management, event streams, and sandbox review.",
    points: [
      "Renders normalized thinking, text, tool, and subagent events in real time.",
      "Provides session lists, agent selection, sandbox binding, shell, files, and noVNC views.",
      "Depends on application REST and WebSocket contracts instead of model SDK internals.",
    ],
    icon: MessageSquareCode,
  },
  {
    id: "api",
    label: "FastAPI API",
    role: "API Layer",
    detail: "The API layer owns authentication, resource contracts, WebSocket entry points, and service boundaries.",
    points: [
      "Exposes REST resources for users, work projects, sandbox images, containers, agents, and sessions.",
      "Routes WebSocket turns into the active session pool and streams normalized events back to the frontend.",
      "Keeps request validation and response shaping outside the agent runtime.",
    ],
    icon: Braces,
  },
  {
    id: "runtime",
    label: "Agent Runtime",
    role: "Orchestration Layer",
    detail: "The runtime coordinates session lifecycle, context projection, event normalization, cancellation, and compaction.",
    points: [
      "Creates or resumes sessions through AgentSessionPool and persists turn state.",
      "Projects shared history into role-specific views before model execution.",
      "Normalizes SDK events into stable application events and handles interruption or cleanup.",
    ],
    icon: Workflow,
  },
  {
    id: "agentGraph",
    label: "Session Agent Graph",
    role: "Capability Layer",
    detail: "AgentRegistry assembles a session-scoped graph from role specifications, knowledge, tools, model settings, and sandbox state.",
    points: [
      "Binds the coordinator and specialist agents to the current session.",
      "Mounts command tools only when an authorized running sandbox is available.",
      "Keeps specialist delegation, knowledge, and tool access scoped to the assessment context.",
    ],
    icon: GitBranch,
  },
  {
    id: "record",
    label: "Assessment Record",
    role: "Review Layer",
    detail: "The persisted record connects conclusions to streamed events, tool evidence, subagent output, and durable facts.",
    points: [
      "Keeps messages, metadata, delegated jobs, notifications, and stable facts reviewable.",
      "Supports resumed reviews and post-assessment review.",
      "Helps operators distinguish confirmed findings, assumptions, residual risk, and next actions.",
    ],
    icon: FileCheck2,
  },
  {
    id: "sandbox",
    label: "Docker Sandbox",
    role: "Execution Layer",
    detail: "Sandbox containers provide the controlled execution boundary for agent tools and manual user review.",
    points: [
      "Runs commands, skills, shell sessions, browser workflows, file operations, and noVNC access.",
      "Returns structured command results to agents while preserving a user review path.",
      "Invalidates tool bindings when container state changes.",
    ],
    icon: Box,
  },
  {
    id: "tools",
    label: "Tool Surface",
    role: "Tool Layer",
    detail: "Tool mounting translates sandbox, knowledge, and skill capabilities into explicit agent-callable interfaces.",
    points: [
      "Separates unavailable tools from the active agent graph.",
      "Supports synchronous commands, async command jobs, skills, and knowledge loading.",
      "Keeps command output structured so it can be reasoned over and replayed.",
    ],
    icon: SquareTerminal,
  },
  {
    id: "model",
    label: "Model Providers",
    role: "Model Layer",
    detail: "Model access stays behind role and runtime boundaries with configurable native OpenAI-compatible provider modes.",
    points: [
      "Allows model routing to be configured outside frontend code.",
      "Keeps provider details behind agent and runtime interfaces.",
      "Supports different model choices for coordinator and specialist roles.",
    ],
    icon: Bot,
  },
  {
    id: "store",
    label: "PostgreSQL Store",
    role: "Persistence Layer",
    detail: "PostgreSQL stores sessions, messages, metadata, delegated jobs, sandbox records, users, and work projects.",
    points: [
      "Persists long-running assessments across browser refreshes and runtime recovery.",
      "Stores subagent job state, completion notifications, and review metadata.",
      "Provides the durable source for replay, compaction, and operational audit.",
    ],
    icon: Database,
  },
  {
    id: "eventContract",
    label: "Event Contract",
    role: "Streaming Layer",
    detail: "The event contract decouples frontend rendering from model and agent SDK internals.",
    points: [
      "Uses stable event types such as thinking_delta, text_delta, tool_call, and tool_result.",
      "Carries subagent task updates and runtime notifications through one frontend protocol.",
      "Lets backend implementation details evolve without changing the workbench event model.",
    ],
    icon: Activity,
  },
];

const mainArchitectureNodeIds = ["operator", "workbench", "api", "runtime", "agentGraph", "record"];
const executionLayerNodeIds = ["sandbox", "tools", "model"];
const foundationLayerNodeIds = ["store", "eventContract"];

const agents = [
  {
    code: "cso",
    name: "Z3r0",
    role: "Chief Security Officer",
    capability: "Coordination",
    direction: "Assessment planning, delegation, and synthesis",
    detail: "Task decomposition, team coordination, and result integration.",
    accent: "red",
    icon: Workflow,
  },
  {
    code: "cae",
    name: "V3ra",
    role: "Chief Audit Engineer",
    capability: "Code Audit",
    direction: "Source, dependency, and configuration review",
    detail: "Source code security audit, dependency review, and remediation verification.",
    accent: "cyan",
    icon: ClipboardCheck,
  },
  {
    code: "cie",
    name: "L1ly",
    role: "Chief Intelligence Engineer",
    capability: "Intelligence",
    direction: "Authorized asset context and relationship analysis",
    detail: "Intelligence collection, asset mapping, and relationship analysis.",
    accent: "red",
    icon: FileSearch,
  },
  {
    code: "cpe",
    name: "Fr4nk",
    role: "Chief Penetration Engineer",
    capability: "Validation",
    direction: "Authorized validation and risk verification",
    detail: "Penetration testing, vulnerability validation, and risk verification.",
    accent: "cyan",
    icon: ShieldCheck,
  },
  {
    code: "cre",
    name: "J4m3",
    role: "Chief Reverse Engineer",
    capability: "Reverse",
    direction: "Sample, binary, firmware, and APK analysis",
    detail: "File, binary, firmware, and APK reverse engineering.",
    accent: "red",
    icon: Code2,
  },
  {
    code: "cce",
    name: "Nu1L",
    role: "Chief Cryptography Engineer",
    capability: "Cryptography",
    direction: "Protocol, key management, and implementation review",
    detail: "Cryptographic protocol review, key management, and implementation analysis.",
    accent: "cyan",
    icon: LockKeyhole,
  },
];

const runtimeSteps = [
  { title: "Receive", text: "The operator submits a brief, target agent, and optional sandbox binding.", icon: MessageSquareCode },
  { title: "Resume", text: "AgentSessionPool creates or resumes the active session.", icon: Layers3 },
  { title: "Project", text: "Z3r0Session loads the scoped history view for each agent.", icon: FileSearch },
  { title: "Stream", text: "Runtime output becomes thinking, text, tool, and subagent events.", icon: Activity },
  { title: "Persist", text: "Messages, metadata, and durable facts are stored for replay.", icon: Database },
];

const highlights = [
  ["Session-level Agent Graph", "Roles, tools, knowledge, and subagents are bound dynamically for each assessment session."],
  ["Persistent Delegation Jobs", "Specialist work can run in the background, recover from stale state, and notify the coordinator."],
  ["Viewer-specific Projection", "Agents share persisted history while receiving context scoped to their responsibility."],
  ["Long-context Compaction", "Earlier history is summarized while recent context and durable facts remain available."],
  ["Stable Streaming Contract", "Frontend event schemas stay independent from model SDK internals."],
  ["Sandbox Tool Invalidation", "Sandbox status changes invalidate tool bindings and clean up active jobs."],
];

const sandboxTools = ["Commands", "Skills", "Shell", "Files", "noVNC", "Ghidra", "jadx", "sqlmap", "nmap"];

export function LandingContent({ logoSrc, primaryAction }: LandingContentProps) {
  const [activeNode, setActiveNode] = useState(architectureNodes[3]);
  const ActiveArchitectureIcon = activeNode.icon;
  const mainArchitectureNodes = mainArchitectureNodeIds.map(getArchitectureNode);
  const executionLayerNodes = executionLayerNodeIds.map(getArchitectureNode);
  const foundationLayerNodes = foundationLayerNodeIds.map(getArchitectureNode);

  return (
    <main className="landing-page">
      <div className="landing-grid" aria-hidden="true" />
      <div className="landing-scanline" aria-hidden="true" />

      <section id="top" className="landing-hero" aria-label="Z3r0 landing page">
        <div className="landing-hero-copy">
          <div className="landing-title-row">
            <img className="landing-hero-logo" src={logoSrc} width="1000" height="1000" alt="Z3r0 logo" />
            <div>
              <h1>Z3r0 Multi-Agent Security Workbench</h1>
              <p>
                A controlled multi-agent workbench for authorized security assessments,
                code auditing, internal review, and controlled research.
              </p>
            </div>
          </div>
          <div className="landing-actions">
            <PrimaryActionLink action={primaryAction} />
            <a className="landing-action-link landing-action-secondary" href="#architecture">
              <Network size={17} />
              <span>View architecture</span>
            </a>
            <a className="landing-action-link landing-action-secondary" href={repositoryUrl} target="_blank" rel="noopener noreferrer">
              <GitBranch size={17} />
              <span>Follow us</span>
            </a>
          </div>
        </div>

        <div className="landing-capability-matrix" aria-label="Z3r0 capability matrix">
          <div className="landing-capability-header">
            <span className="page-eyebrow">Operating Model</span>
            <strong>
              Coordinator-led work with
              <span>specialist execution and review paths.</span>
            </strong>
          </div>
          <div className="landing-capability-disclaimer">
            <div className="landing-boundary-heading">
              <AlertTriangle size={18} />
              <h3>Authorized use only</h3>
            </div>
            <p>
              Use this project only within a lawful and explicitly authorized scope. It does not grant
              permission to test, access, scan, or affect any third-party system, network, service, account,
              or data. Unauthorized, unlawful, or harmful use is prohibited. Users are responsible for
              preserving authorization, defining scope, and complying with applicable laws, contracts, and
              authorization boundaries. The author is not responsible for any consequence, loss, damage,
              legal liability, or unlawful act caused by users.
            </p>
          </div>
          <div className="landing-capability-grid">
            {agents.map((agent) => {
              const Icon = agent.icon;
              return (
                <article key={agent.code} className={`landing-capability-cell landing-capability-cell-${agent.accent}`}>
                  <div className="landing-capability-title">
                    <Icon size={20} />
                    <h3>{agent.capability}</h3>
                  </div>
                  <div className="landing-capability-identity">
                    <span>{agent.code}</span>
                    <strong>{agent.name}</strong>
                  </div>
                  <p>{agent.direction}</p>
                </article>
              );
            })}
          </div>
        </div>
      </section>

      <section id="architecture" className="landing-section landing-architecture" aria-labelledby="architecture-title">
        <div className="landing-section-heading">
          <span className="page-eyebrow">Architecture</span>
          <h2 id="architecture-title">Layered architecture for governed agent operations.</h2>
          <p>Z3r0 separates the user-facing surface, API boundary, runtime orchestration, session agent graph, controlled execution, model access, streaming protocol, and persisted assessment record.</p>
        </div>

        <div className="landing-architecture-layout">
          <div className="landing-architecture-map" aria-label="Z3r0 layered architecture">
            <div className="landing-architecture-flow" aria-label="Primary request and review path">
              {mainArchitectureNodes.map((node) => (
                <ArchitectureGraphNode
                  key={node.id}
                  node={node}
                  activeId={activeNode.id}
                  className={`landing-arch-node-${node.id}`}
                  onSelect={setActiveNode}
                  emphasized={node.id === "runtime" || node.id === "agentGraph"}
                />
              ))}
            </div>

            <div className="landing-architecture-layer" aria-label="Execution and model layer">
              <div className="landing-layer-title">
                <span>Execution</span>
                <strong>Tools, sandbox, and models are mounted behind runtime authorization.</strong>
              </div>
              <div className="landing-layer-grid landing-layer-grid-execution">
                {executionLayerNodes.map((node) => (
                  <ArchitectureGraphNode key={node.id} node={node} activeId={activeNode.id} onSelect={setActiveNode} compact />
                ))}
              </div>
            </div>

            <div className="landing-architecture-layer landing-architecture-layer-foundation" aria-label="Persistence and streaming layer">
              <div className="landing-layer-title">
                <span>Foundation</span>
                <strong>Durable records and stable events keep long assessments reviewable.</strong>
              </div>
              <div className="landing-layer-grid landing-layer-grid-foundation">
                {foundationLayerNodes.map((node) => (
                  <ArchitectureGraphNode key={node.id} node={node} activeId={activeNode.id} onSelect={setActiveNode} compact />
                ))}
              </div>
            </div>
          </div>

          <aside className="landing-architecture-detail">
            <div className="landing-detail-heading">
              <div className="landing-detail-icon">
                <ActiveArchitectureIcon size={24} />
              </div>
              <div>
                <span className="page-eyebrow">Selected layer</span>
                <h3>{activeNode.label}</h3>
              </div>
            </div>
            <strong className="landing-detail-role">{activeNode.role}</strong>
            <p>{activeNode.detail}</p>
            <ul className="landing-detail-points">
              {activeNode.points.map((point) => (
                <li key={point}>{point}</li>
              ))}
            </ul>
          </aside>
        </div>
      </section>

      <section id="agents" className="landing-section" aria-labelledby="agents-title">
        <div className="landing-section-heading">
          <span className="page-eyebrow">Agent Team</span>
          <h2 id="agents-title">A lead security role coordinates specialists across the assessment lifecycle.</h2>
        </div>
        <div className="landing-agent-grid">
          {agents.map((agent) => (
            <article key={agent.code} className={`landing-agent-card landing-agent-card-${agent.accent}`}>
              <div>
                <span>{agent.code}</span>
                <strong>{agent.name}</strong>
              </div>
              <h3>{agent.role}</h3>
              <p>{agent.detail}</p>
            </article>
          ))}
        </div>
      </section>

      <section id="runtime" className="landing-section landing-runtime" aria-labelledby="runtime-title">
        <div className="landing-section-heading">
          <span className="page-eyebrow">Runtime Flow</span>
          <h2 id="runtime-title">Streaming sessions remain replayable, cancellable, and maintainable during long reviews.</h2>
        </div>

        <div className="landing-runtime-track">
          {runtimeSteps.map(({ icon: Icon, title, text }, index) => (
            <article key={title} className="landing-runtime-step">
              <div className="landing-runtime-heading">
                <div className="landing-runtime-title">
                  <Icon size={18} />
                  <h3>{title}</h3>
                </div>
                <span>{String(index + 1).padStart(2, "0")}</span>
              </div>
              <p>{text}</p>
            </article>
          ))}
        </div>

        <div className="landing-sandbox-panel">
          <div>
            <span className="page-eyebrow">Sandbox Tooling</span>
            <h3>Agent tools and manual review share the same controlled execution boundary.</h3>
            <p>Agents receive structured command results while users can open shell, screen, and file manager views for validation and review within an authorized scope.</p>
          </div>
          <div className="landing-tool-cloud">
            {sandboxTools.map((tool) => <span key={tool}>{tool}</span>)}
          </div>
        </div>
      </section>

      <section className="landing-section landing-highlights" aria-labelledby="highlights-title">
        <div className="landing-section-heading">
          <span className="page-eyebrow">Technical Characteristics</span>
          <h2 id="highlights-title">Runtime boundaries designed for controlled and reviewable security operations.</h2>
        </div>
        <div className="landing-highlight-grid">
          {highlights.map(([title, text], index) => (
            <article key={title} className="landing-highlight-card">
              <div className="landing-highlight-heading">
                {index % 2 === 0 ? <Zap size={18} /> : <CheckCircle2 size={18} />}
                <h3>{title}</h3>
              </div>
              <p>{text}</p>
            </article>
          ))}
        </div>
      </section>

      <section id="security" className="landing-section landing-security" aria-labelledby="security-title">
        <div className="landing-section-heading">
          <span className="page-eyebrow">Operational Boundary</span>
          <h2 id="security-title">Built for authorized assessments in controlled enterprise environments.</h2>
          <p>Use Z3r0 where sandbox execution, Docker access, file operations, and model credentials can be governed as high-privilege assets.</p>
        </div>
        <div className="landing-boundary">
          <div className="landing-boundary-heading">
            <LockKeyhole size={20} />
            <h3>Trusted deployment required</h3>
          </div>
          <p>
            Z3r0 is intended for authorized security assessment, code auditing,
            internal review, controlled research, and training environments. Network access,
            sandbox containers, terminal access, file management, and model
            credentials should remain isolated and trusted. Users must define and
            follow an explicit authorization scope before using any tool capability.
          </p>
        </div>
      </section>
    </main>
  );
}

function PrimaryActionLink({ action }: { action: LandingPrimaryAction }) {
  if (action.href) {
    return (
      <a
        className="landing-action-link landing-action-primary"
        href={action.href}
        target={action.external ? "_blank" : undefined}
        rel={action.external ? "noopener noreferrer" : undefined}
      >
        <ShieldCheck size={17} />
        <span>{action.label}</span>
      </a>
    );
  }

  return (
    <button className="landing-action-link landing-action-primary" type="button" onClick={action.onSelect}>
      <ShieldCheck size={17} />
      <span>{action.label}</span>
    </button>
  );
}

function getArchitectureNode(id: string) {
  const node = architectureNodes.find((item) => item.id === id);
  if (!node) {
    throw new Error(`Missing architecture node: ${id}`);
  }
  return node;
}

function ArchitectureGraphNode({
  activeId,
  className = "",
  compact = false,
  emphasized = false,
  node,
  onSelect,
}: {
  activeId: string;
  className?: string;
  compact?: boolean;
  emphasized?: boolean;
  node: ArchitectureNode;
  onSelect: (node: ArchitectureNode) => void;
}) {
  const Icon = node.icon;
  const isActive = activeId === node.id;

  return (
    <button
      className={[
        "landing-arch-node",
        compact ? "landing-arch-node-compact" : "",
        emphasized ? "landing-arch-node-emphasized" : "",
        className,
        isActive ? "active" : "",
      ].filter(Boolean).join(" ")}
      type="button"
      onClick={() => onSelect(node)}
      onFocus={() => onSelect(node)}
      onMouseEnter={() => onSelect(node)}
    >
      <Icon size={compact ? 16 : 18} />
      <span>{node.label}</span>
      <em>{node.role}</em>
    </button>
  );
}
