import { Avatar, Button } from "@douyinfe/semi-ui";
import { Box, Boxes, FolderKanban, LogOut, MessageSquareCode, ShieldCheck, Users } from "lucide-react";
import { ReactNode, useCallback, useState } from "react";
import { NavLink, Outlet, useLocation, useNavigate, useOutletContext } from "react-router-dom";
import { SessionList } from "../../features/playground/SessionList";
import { useAgentSessionContext } from "../../features/playground/AgentSessionProvider";
import { useAuth } from "../../shared/auth/AuthProvider";

type AdminLayoutContext = { setHeaderActions: (actions: ReactNode) => void };

export function useAdminHeaderActions() {
  return useOutletContext<AdminLayoutContext>().setHeaderActions;
}

const navItems = [
  { path: "/playground", label: "Playground", eyebrow: "Agent Workbench", icon: MessageSquareCode },
  { path: "/work-projects", label: "Work Projects", eyebrow: "Project Operations", icon: FolderKanban },
  { path: "/sandbox-images", label: "Sandbox Images", eyebrow: "Execution Baseline", icon: Boxes },
  { path: "/sandbox-containers", label: "Sandbox Containers", eyebrow: "Runtime Instances", icon: Box },
  { path: "/system-users", label: "System Users", eyebrow: "Access Control", icon: Users },
];

export function AdminLayout() {
  const { signOut } = useAuth();
  const navigate = useNavigate();
  const location = useLocation();
  const [headerActions, setHeaderActionsState] = useState<ReactNode>(null);
  const { sessions, sessionsLoading, agents, activeSessionId, selectSession, deleteSession } = useAgentSessionContext();

  const setHeaderActions = useCallback((actions: ReactNode) => {
    setHeaderActionsState(() => actions);
  }, []);

  const handleSelectAgentSession = useCallback((sessionId: string) => {
    selectSession(sessionId);
    if (!location.pathname.startsWith("/playground")) {
      navigate("/playground");
    }
  }, [location.pathname, navigate, selectSession]);

  const outletContext: AdminLayoutContext = { setHeaderActions };

  const handleSignOut = () => {
    signOut();
    navigate("/login", { replace: true });
  };

  const activeItem = navItems.find((item) => location.pathname.startsWith(item.path));

  return (
    <div className="admin-shell">
      <aside className="admin-sidebar">
        <div className="brand-lockup">
          <div className="brand-mark"><ShieldCheck size={22} /></div>
          <div>
            <div className="brand-name">Z3r0</div>
            <div className="brand-kicker">Red Team Collaboration Platform</div>
          </div>
        </div>

        <div className="admin-sidebar-body">
          <div className="admin-sidebar-top">
            <NavLink to="/playground" className="admin-nav-link">
              <MessageSquareCode size={18} />
              <span>Playground</span>
            </NavLink>
            <div className="admin-sidebar-secondary">
              <SessionList
                sessions={sessions}
                agents={agents}
                loading={sessionsLoading}
                activeSessionId={activeSessionId}
                onSelect={handleSelectAgentSession}
                onDelete={deleteSession}
              />
            </div>
          </div>

          <nav className="admin-nav admin-nav-bottom" aria-label="Primary navigation">
            {navItems.slice(1).map((item) => {
              const Icon = item.icon;
              return (
                <NavLink key={item.path} to={item.path} className="admin-nav-link">
                  <Icon size={18} />
                  <span>{item.label}</span>
                </NavLink>
              );
            })}
          </nav>
        </div>
      </aside>

      <div className="admin-main">
        <header className="admin-topbar">
          <div>
            <div className="page-eyebrow">{activeItem?.eyebrow || "Operations"}</div>
            <h1>{activeItem?.label || "Console"}</h1>
          </div>
          <div className="topbar-actions">
            {headerActions ? <div className="topbar-resource-actions">{headerActions}</div> : null}
            <div className="topbar-session-actions">
              <Avatar size="small" color="red">A</Avatar>
              <Button icon={<LogOut size={16} />} theme="borderless" onClick={handleSignOut} aria-label="Sign out" />
            </div>
          </div>
        </header>
        <main className="admin-content">
          <Outlet context={outletContext} />
        </main>
      </div>
    </div>
  );
}
