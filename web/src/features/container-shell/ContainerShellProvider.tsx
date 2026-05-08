import { Button } from "@douyinfe/semi-ui";
import { FitAddon } from "@xterm/addon-fit";
import { Terminal } from "@xterm/xterm";
import "@xterm/xterm/css/xterm.css";
import { Maximize2, Minimize2, Minus, Monitor, FolderOpen, SquareTerminal, X } from "lucide-react";
import {
  CSSProperties,
  createContext,
  Dispatch,
  ReactNode,
  useCallback,
  useContext,
  useEffect,
  MutableRefObject,
  useMemo,
  PointerEvent as ReactPointerEvent,
  useRef,
  useState,
  SetStateAction,
} from "react";
import { buildContainerNoVNCUrl, buildContainerShellUrl } from "../../shared/api/sandboxContainers";
import { showApiError } from "../../shared/api/feedback";
import type { SandboxContainer } from "../../shared/api/types";
import { ContainerFileManager } from "./ContainerFileManager";

type ShellStatus = "idle" | "connecting" | "open" | "closed";
type ShellDockState = "normal" | "minimized";

type ShellRect = {
  x: number;
  y: number;
  width: number;
  height: number;
};

type FloatingWindowStateBase = ShellRect & {
  containerName: string;
  dockState: ShellDockState;
};

type ShellWindowState = FloatingWindowStateBase & {
  connectionKey: number;
  containerHash: string;
  status: ShellStatus;
  isMaximized: boolean;
  restoreRect: ShellRect | null;
};

type NoVNCWindowState = FloatingWindowStateBase & {
  containerId: number;
  url: string;
};

type FileManagerWindowState = FloatingWindowStateBase & {
  containerId: number;
  containerHash: string;
};

type ContainerShellContextValue = {
  openFileManager: (container: SandboxContainer) => void;
  openNoVNC: (container: SandboxContainer) => void;
  openShell: (container: SandboxContainer) => void;
};

type ShellFlightState = {
  direction: "minimize" | "restore";
  containerName: string;
  meta: string;
  from: ShellRect;
  to: ShellRect;
};

type FloatingWindowDockSlot = "shell" | "novnc" | "filemanager";

type WindowDragState = {
  x: number;
  y: number;
  startX: number;
  startY: number;
};

type WindowResizeState = {
  target: "shell" | "filemanager";
  width: number;
  height: number;
  startX: number;
  startY: number;
};

type FloatingWindowProps = {
  actions: ReactNode;
  children: ReactNode;
  className?: string;
  dockState: ShellDockState;
  icon: ReactNode;
  isMaximized?: boolean;
  meta: string;
  onHeaderPointerDown: (event: ReactPointerEvent<HTMLDivElement>) => void;
  rect: ShellRect;
  resizeHandle?: ReactNode;
  title: string;
};

type FloatingWindowFlightProps = {
  flight: ShellFlightState;
  frameRef: MutableRefObject<HTMLDivElement | null>;
  icon: ReactNode;
  style?: CSSProperties;
};

type MinimizedWindowButtonProps = {
  ariaLabel: string;
  className?: string;
  icon: ReactNode;
  onClick: () => void;
};

type FitTerminalOptions = {
  snapHeight?: boolean;
};

const DEFAULT_WIDTH = 760;
const DEFAULT_HEIGHT = 460;
const FLOATING_WINDOW_HEADER_HEIGHT = 42;
const FLOATING_WINDOW_BORDER_WIDTH = 1;
const NOVNC_SCREEN_WIDTH = 1280;
const NOVNC_SCREEN_HEIGHT = 720;
const NOVNC_DEFAULT_WIDTH = NOVNC_SCREEN_WIDTH + (FLOATING_WINDOW_BORDER_WIDTH * 2);
const NOVNC_DEFAULT_HEIGHT = NOVNC_SCREEN_HEIGHT + FLOATING_WINDOW_HEADER_HEIGHT + (FLOATING_WINDOW_BORDER_WIDTH * 2);
const MIN_WIDTH = 420;
const MIN_HEIGHT = 260;
const MAXIMIZED_MARGIN = 12;
const DOCK_BUTTON_RIGHT = 0;
const DOCK_BUTTON_SIZE = 46;
const DOCK_BUTTON_GAP = 54;
const WINDOW_DOCK_TRANSITION_MS = 420;

const ContainerShellContext = createContext<ContainerShellContextValue | null>(null);

export function useContainerShell() {
  const value = useContext(ContainerShellContext);
  if (!value) throw new Error("useContainerShell must be used inside ContainerShellProvider");
  return value;
}

export function ContainerShellProvider({ children }: { children: ReactNode }) {
  const [shell, setShell] = useState<ShellWindowState | null>(null);
  const [shellFlight, setShellFlight] = useState<ShellFlightState | null>(null);
  const [noVNC, setNoVNC] = useState<NoVNCWindowState | null>(null);
  const [noVNCFlight, setNoVNCFlight] = useState<ShellFlightState | null>(null);
  const [fileManager, setFileManager] = useState<FileManagerWindowState | null>(null);
  const [fileManagerFlight, setFileManagerFlight] = useState<ShellFlightState | null>(null);
  const terminalHostRef = useRef<HTMLDivElement | null>(null);
  const terminalRef = useRef<Terminal | null>(null);
  const fitRef = useRef<FitAddon | null>(null);
  const socketRef = useRef<WebSocket | null>(null);
  const shellFlightRef = useRef<HTMLDivElement | null>(null);
  const shellFlightFrameRef = useRef<number | null>(null);
  const noVNCFlightRef = useRef<HTMLDivElement | null>(null);
  const noVNCFlightFrameRef = useRef<number | null>(null);
  const fileManagerFlightRef = useRef<HTMLDivElement | null>(null);
  const fileManagerFlightFrameRef = useRef<number | null>(null);
  const dragRef = useRef<WindowDragState | null>(null);
  const noVNCDragRef = useRef<WindowDragState | null>(null);
  const fileManagerDragRef = useRef<WindowDragState | null>(null);
  const resizeRef = useRef<WindowResizeState | null>(null);
  const fitWithoutSnapRef = useRef(false);
  const connectionKeyRef = useRef(0);
  const activeContainerHash = shell?.containerHash ?? null;
  const activeConnectionKey = shell?.connectionKey ?? null;

  const disposeShellResources = useCallback(() => {
    closeSocket(socketRef.current);
    socketRef.current = null;
    terminalRef.current?.dispose();
    terminalRef.current = null;
    fitRef.current = null;
  }, []);

  const closeShell = useCallback(() => {
    cancelFlightFrame(shellFlightFrameRef);
    setShellFlight(null);
    disposeShellResources();
    setShell(null);
  }, [disposeShellResources]);

  const sendResize = useCallback(() => {
    const terminal = terminalRef.current;
    const socket = socketRef.current;
    if (!terminal || !socket || socket.readyState !== WebSocket.OPEN) return;
    socket.send(JSON.stringify({ type: "resize", rows: terminal.rows, cols: terminal.cols }));
  }, []);

  const fitTerminal = useCallback((options: FitTerminalOptions = {}) => {
    if (!fitRef.current || !terminalRef.current || !terminalHostRef.current) return;
    fitRef.current.fit();
    if (options.snapHeight !== false) {
      snapShellHeightToRows(terminalHostRef.current, terminalRef.current, setShell);
    }
    sendResize();
  }, [sendResize]);

  const toggleMaximizeShell = useCallback(() => {
    cancelFlightFrame(shellFlightFrameRef);
    dragRef.current = null;
    resizeRef.current = null;
    setShellFlight(null);
    fitWithoutSnapRef.current = true;
    setShell((current) => {
      if (!current) return current;
      if (current.isMaximized) {
        const restoreRect = current.restoreRect ?? getShellRect(current);
        return { ...current, ...restoreRect, isMaximized: false, restoreRect: null };
      }

      return {
        ...current,
        ...getMaximizedShellRect(),
        isMaximized: true,
        restoreRect: getShellRect(current),
      };
    });
  }, [fitTerminal]);

  const minimizeShell = useCallback(() => {
    if (!shell) return;
    cancelFlightFrame(shellFlightFrameRef);
    setShellFlight(buildShellFlight(shell, "minimize"));
    setShell((current) => current ? { ...current, dockState: "minimized" } : current);
  }, [shell]);

  const restoreShell = useCallback(() => {
    if (!shell) return;
    cancelFlightFrame(shellFlightFrameRef);
    setShellFlight(buildShellFlight(shell, "restore"));
  }, [shell]);

  const openShell = useCallback((container: SandboxContainer) => {
    if (!container.container_hash) return;

    const currentShell = shell;
    if (currentShell?.containerHash === container.container_hash && isSocketActive(socketRef.current)) {
      const preserveGeometry = currentShell.dockState === "minimized";
      cancelFlightFrame(shellFlightFrameRef);
      setShellFlight(null);
      fitWithoutSnapRef.current = preserveGeometry;
      setShell((current) => current ? {
        ...current,
        containerName: container.container_name,
        dockState: "normal",
      } : current);
      window.setTimeout(() => {
        fitTerminal({ snapHeight: !preserveGeometry });
        terminalRef.current?.focus();
      }, 0);
      return;
    }

    cancelFlightFrame(shellFlightFrameRef);
    setShellFlight(null);
    disposeShellResources();

    setShell({
      connectionKey: connectionKeyRef.current + 1,
      containerHash: container.container_hash,
      containerName: container.container_name,
      dockState: "normal",
      status: "connecting",
      isMaximized: false,
      restoreRect: null,
      x: Math.max(24, window.innerWidth - DEFAULT_WIDTH - 36),
      y: Math.max(92, window.innerHeight - DEFAULT_HEIGHT - 36),
      width: DEFAULT_WIDTH,
      height: DEFAULT_HEIGHT,
    });
    connectionKeyRef.current += 1;
  }, [disposeShellResources, fitTerminal, shell]);

  const closeNoVNC = useCallback(() => {
    cancelFlightFrame(noVNCFlightFrameRef);
    noVNCDragRef.current = null;
    setNoVNCFlight(null);
    setNoVNC(null);
  }, []);

  const minimizeNoVNC = useCallback(() => {
    if (!noVNC) return;
    cancelFlightFrame(noVNCFlightFrameRef);
    setNoVNCFlight(buildNoVNCFlight(noVNC, "minimize"));
    setNoVNC((current) => current ? { ...current, dockState: "minimized" } : current);
  }, [noVNC]);

  const restoreNoVNC = useCallback(() => {
    if (!noVNC) return;
    cancelFlightFrame(noVNCFlightFrameRef);
    setNoVNCFlight(buildNoVNCFlight(noVNC, "restore"));
  }, [noVNC]);

  const openNoVNC = useCallback((container: SandboxContainer) => {
    try {
      const url = buildContainerNoVNCUrl(container);
      cancelFlightFrame(noVNCFlightFrameRef);
      setNoVNCFlight(null);
      setNoVNC((current) => {
        if (current?.containerId === container.id && current.url === url) {
          return { ...current, containerName: container.container_name, dockState: "normal" };
        }

        return {
          containerId: container.id,
          containerName: container.container_name,
          dockState: "normal",
          url,
          ...getInitialNoVNCRect(),
        };
      });
    } catch (error) {
      showApiError(error);
    }
  }, []);

  const closeFileManager = useCallback(() => {
    cancelFlightFrame(fileManagerFlightFrameRef);
    fileManagerDragRef.current = null;
    setFileManagerFlight(null);
    setFileManager(null);
  }, []);

  const minimizeFileManager = useCallback(() => {
    if (!fileManager) return;
    cancelFlightFrame(fileManagerFlightFrameRef);
    setFileManagerFlight(buildFileManagerFlight(fileManager, "minimize"));
    setFileManager((current) => current ? { ...current, dockState: "minimized" } : current);
  }, [fileManager]);

  const restoreFileManager = useCallback(() => {
    if (!fileManager) return;
    cancelFlightFrame(fileManagerFlightFrameRef);
    setFileManagerFlight(buildFileManagerFlight(fileManager, "restore"));
  }, [fileManager]);

  const openFileManager = useCallback((container: SandboxContainer) => {
    if (!container.container_hash) return;

    cancelFlightFrame(fileManagerFlightFrameRef);
    setFileManagerFlight(null);
    setFileManager((current) => {
      if (current?.containerId === container.id) {
        return { ...current, containerName: container.container_name, dockState: "normal" };
      }

      return {
        containerId: container.id,
        containerHash: container.container_hash,
        containerName: container.container_name,
        dockState: "normal",
        ...getInitialFileManagerRect(),
      };
    });
  }, []);

  useEffect(() => {
    if (!activeContainerHash || activeConnectionKey === null || terminalRef.current || !terminalHostRef.current) return;

    const terminal = new Terminal({
      cursorBlink: true,
      convertEol: true,
      fontFamily: "JetBrains Mono, SFMono-Regular, Consolas, monospace",
      fontSize: 13,
      theme: {
        background: "#0b111c",
        foreground: "#d8e1ec",
        cursor: "#ffffff",
        selectionBackground: "#35506e",
      },
    });
    const fit = new FitAddon();
    terminal.loadAddon(fit);
    terminal.open(terminalHostRef.current);
    terminalRef.current = terminal;
    fitRef.current = fit;
    window.setTimeout(fitTerminal, 0);

    let socket: WebSocket;
    try {
      socket = new WebSocket(buildContainerShellUrl(activeContainerHash));
    } catch (error) {
      terminal.dispose();
      terminalRef.current = null;
      fitRef.current = null;
      showApiError(error);
      setShell((current) => current ? { ...current, status: "closed" } : current);
      return;
    }

    socket.binaryType = "arraybuffer";
    socketRef.current = socket;

    const disposable = terminal.onData((data) => {
      if (socket.readyState === WebSocket.OPEN) {
        socket.send(JSON.stringify({ type: "input", data }));
      }
    });

    socket.addEventListener("open", () => {
      setShell((current) => current ? { ...current, status: "open" } : current);
      terminal.focus();
      fitTerminal({ snapHeight: false });
    });
    socket.addEventListener("message", (event) => {
      if (typeof event.data === "string") {
        terminal.write(event.data);
        return;
      }
      const decoder = new TextDecoder();
      terminal.write(decoder.decode(event.data as ArrayBuffer));
    });
    socket.addEventListener("close", () => {
      setShell((current) => current ? { ...current, status: "closed" } : current);
    });
    socket.addEventListener("error", () => {
      setShell((current) => current ? { ...current, status: "closed" } : current);
    });

    return () => {
      disposable.dispose();
      socket.close();
      terminal.dispose();
      if (socketRef.current === socket) socketRef.current = null;
      if (terminalRef.current === terminal) terminalRef.current = null;
      if (fitRef.current === fit) fitRef.current = null;
    };
  }, [activeConnectionKey, activeContainerHash, fitTerminal]);

  useEffect(() => () => closeShell(), [closeShell]);

  useEffect(() => () => closeNoVNC(), [closeNoVNC]);

  useEffect(() => () => closeFileManager(), [closeFileManager]);

  useEffect(() => () => {
    cancelFlightFrame(shellFlightFrameRef);
    cancelFlightFrame(noVNCFlightFrameRef);
    cancelFlightFrame(fileManagerFlightFrameRef);
  }, []);

  useEffect(() => {
    if (!shellFlight || !shellFlightRef.current) return;
    return animateWindowFlight(shellFlightRef.current, shellFlight, shellFlightFrameRef, () => {
      if (shellFlight.direction === "restore") {
        fitWithoutSnapRef.current = true;
        setShell((current) => current ? { ...current, dockState: "normal" } : current);
      }
      setShellFlight(null);
    });
  }, [shellFlight]);

  useEffect(() => {
    if (!noVNCFlight || !noVNCFlightRef.current) return;
    return animateWindowFlight(noVNCFlightRef.current, noVNCFlight, noVNCFlightFrameRef, () => {
      if (noVNCFlight.direction === "restore") {
        setNoVNC((current) => current ? { ...current, dockState: "normal" } : current);
      }
      setNoVNCFlight(null);
    });
  }, [noVNCFlight]);

  useEffect(() => {
    if (!fileManagerFlight || !fileManagerFlightRef.current) return;
    return animateWindowFlight(fileManagerFlightRef.current, fileManagerFlight, fileManagerFlightFrameRef, () => {
      if (fileManagerFlight.direction === "restore") {
        setFileManager((current) => current ? { ...current, dockState: "normal" } : current);
      }
      setFileManagerFlight(null);
    });
  }, [fileManagerFlight]);

  useEffect(() => {
    if (!shell || shell.dockState !== "normal") return;
    const snapHeight = !fitWithoutSnapRef.current;
    fitWithoutSnapRef.current = false;
    window.setTimeout(() => fitTerminal({ snapHeight }), 0);
  }, [fitTerminal, shell?.dockState, shell?.height, shell?.width]);

  useEffect(() => {
    const onWindowResize = () => {
      setShell((current) => current?.isMaximized ? { ...current, ...getMaximizedShellRect() } : current);
      setNoVNC((current) => current ? {
        ...current,
        x: clamp(current.x, 8, window.innerWidth - 80),
        y: clamp(current.y, 8, window.innerHeight - 80),
      } : current);
      setFileManager((current) => current ? clampWindowToViewport(current) : current);
      if (shell?.dockState === "normal") window.setTimeout(fitTerminal, 0);
    };
    window.addEventListener("resize", onWindowResize);
    return () => window.removeEventListener("resize", onWindowResize);
  }, [fitTerminal, shell?.dockState]);

  const onPointerMove = useCallback((event: PointerEvent) => {
    const drag = dragRef.current;
    if (drag) {
      setShell((current) => current ? {
        ...current,
        ...getDraggedWindowPosition(drag, event),
      } : current);
      return;
    }

    const noVNCDrag = noVNCDragRef.current;
    if (noVNCDrag) {
      setNoVNC((current) => current ? {
        ...current,
        ...getDraggedWindowPosition(noVNCDrag, event),
      } : current);
      return;
    }

    const fmDrag = fileManagerDragRef.current;
    if (fmDrag) {
      setFileManager((current) => current ? {
        ...current,
        ...getDraggedWindowPosition(fmDrag, event),
      } : current);
      return;
    }

    const resize = resizeRef.current;
    if (resize) {
      const nextSize = getResizedWindowSize(resize, event);
      if (resize.target === "shell") {
        setShell((current) => current ? { ...current, ...nextSize } : current);
        return;
      }
      setFileManager((current) => current ? { ...current, ...nextSize } : current);
    }
  }, []);

  const stopPointerAction = useCallback(() => {
    dragRef.current = null;
    noVNCDragRef.current = null;
    fileManagerDragRef.current = null;
    resizeRef.current = null;
  }, []);

  useEffect(() => {
    window.addEventListener("pointermove", onPointerMove);
    window.addEventListener("pointerup", stopPointerAction);
    return () => {
      window.removeEventListener("pointermove", onPointerMove);
      window.removeEventListener("pointerup", stopPointerAction);
    };
  }, [onPointerMove, stopPointerAction]);

  const contextValue = useMemo<ContainerShellContextValue>(() => ({ openFileManager, openNoVNC, openShell }), [openFileManager, openNoVNC, openShell]);
  const shellFlightStyle = shellFlight ? buildWindowFlightStyle(shellFlight) : undefined;
  const noVNCFlightStyle = noVNCFlight ? buildWindowFlightStyle(noVNCFlight) : undefined;
  const fileManagerFlightStyle = fileManagerFlight ? buildWindowFlightStyle(fileManagerFlight) : undefined;

  return (
    <ContainerShellContext.Provider value={contextValue}>
      {children}
      {shell ? (
        <>
          <FloatingWindow
            actions={(
              <>
                <Button icon={<Minus size={14} />} theme="borderless" onClick={minimizeShell} aria-label="Minimize shell" />
                <Button icon={shell.isMaximized ? <Minimize2 size={14} /> : <Maximize2 size={14} />} theme="borderless" onClick={toggleMaximizeShell} aria-label={shell.isMaximized ? "Restore shell size" : "Maximize shell"} />
                <Button icon={<X size={14} />} theme="borderless" type="danger" onClick={closeShell} aria-label="Close shell" />
              </>
            )}
            dockState={shell.dockState}
            icon={<SquareTerminal size={16} />}
            isMaximized={shell.isMaximized}
            meta={shell.status}
            rect={shell}
            title={shell.containerName}
            onHeaderPointerDown={(event) => {
              if (shell.isMaximized) return;
              dragRef.current = beginWindowDrag(shell, event);
            }}
            resizeHandle={(
              <div
                className="shell-resize-handle"
                onPointerDown={(event) => {
                  if (shell.isMaximized) return;
                  resizeRef.current = beginWindowResize("shell", shell, event);
                }}
              />
            )}
          >
            <div ref={terminalHostRef} className="shell-terminal" />
          </FloatingWindow>
          {shell.dockState === "minimized" && !shellFlight ? (
            <MinimizedWindowButton ariaLabel="Restore shell" icon={<SquareTerminal size={20} />} onClick={restoreShell} />
          ) : null}
          {shellFlight ? (
            <FloatingWindowFlight frameRef={shellFlightRef} flight={shellFlight} icon={<SquareTerminal size={15} />} style={shellFlightStyle} />
          ) : null}
        </>
      ) : null}
      {noVNC ? (
        <>
          <FloatingWindow
            actions={(
              <>
                <Button icon={<Minus size={14} />} theme="borderless" onClick={minimizeNoVNC} aria-label="Minimize noVNC" />
                <Button icon={<X size={14} />} theme="borderless" type="danger" onClick={closeNoVNC} aria-label="Close noVNC" />
              </>
            )}
            dockState={noVNC.dockState}
            icon={<Monitor size={16} />}
            meta="screen"
            rect={noVNC}
            title={noVNC.containerName}
            onHeaderPointerDown={(event) => {
              noVNCDragRef.current = beginWindowDrag(noVNC, event, { capturePointer: true });
            }}
          >
            <div className="novnc-body">
              <iframe className="novnc-frame" src={noVNC.url} title={`noVNC ${noVNC.containerName}`} />
            </div>
          </FloatingWindow>
          {noVNC.dockState === "minimized" && !noVNCFlight ? (
            <MinimizedWindowButton className="novnc-minimized-button" ariaLabel="Restore noVNC" icon={<Monitor size={20} />} onClick={restoreNoVNC} />
          ) : null}
          {noVNCFlight ? (
            <FloatingWindowFlight frameRef={noVNCFlightRef} flight={noVNCFlight} icon={<Monitor size={15} />} style={noVNCFlightStyle} />
          ) : null}
        </>
      ) : null}
      {fileManager ? (
        <>
          <FloatingWindow
            actions={(
              <>
                <Button icon={<Minus size={14} />} theme="borderless" onClick={minimizeFileManager} aria-label="Minimize file manager" />
                <Button icon={<X size={14} />} theme="borderless" type="danger" onClick={closeFileManager} aria-label="Close file manager" />
              </>
            )}
            dockState={fileManager.dockState}
            icon={<FolderOpen size={16} />}
            meta="files"
            rect={fileManager}
            title={fileManager.containerName}
            onHeaderPointerDown={(event) => {
              fileManagerDragRef.current = beginWindowDrag(fileManager, event, { capturePointer: true });
            }}
            resizeHandle={(
              <div
                className="shell-resize-handle"
                onPointerDown={(event) => {
                  resizeRef.current = beginWindowResize("filemanager", fileManager, event);
                }}
              />
            )}
          >
            <ContainerFileManager
              containerId={fileManager.containerId}
              containerHash={fileManager.containerHash}
              containerName={fileManager.containerName}
            />
          </FloatingWindow>
          {fileManager.dockState === "minimized" && !fileManagerFlight ? (
            <MinimizedWindowButton className="filemanager-minimized-button" ariaLabel="Restore file manager" icon={<FolderOpen size={20} />} onClick={restoreFileManager} />
          ) : null}
          {fileManagerFlight ? (
            <FloatingWindowFlight frameRef={fileManagerFlightRef} flight={fileManagerFlight} icon={<FolderOpen size={15} />} style={fileManagerFlightStyle} />
          ) : null}
        </>
      ) : null}
    </ContainerShellContext.Provider>
  );
}

function FloatingWindow({
  actions,
  children,
  className,
  dockState,
  icon,
  isMaximized = false,
  meta,
  onHeaderPointerDown,
  rect,
  resizeHandle,
  title,
}: FloatingWindowProps) {
  return (
    <div className={buildWindowClassName(className, dockState, isMaximized)} style={buildWindowStyle(rect)}>
      <FloatingWindowHeader
        actions={actions}
        icon={icon}
        meta={meta}
        title={title}
        onPointerDown={onHeaderPointerDown}
      />
      {children}
      {resizeHandle}
    </div>
  );
}

function FloatingWindowHeader({
  actions,
  icon,
  meta,
  onPointerDown,
  title,
}: Pick<FloatingWindowProps, "actions" | "icon" | "meta" | "title"> & {
  onPointerDown: (event: ReactPointerEvent<HTMLDivElement>) => void;
}) {
  return (
    <div className="shell-window-header" onPointerDown={onPointerDown}>
      <div className="shell-window-title">
        {icon}
        <span>{title}</span>
        <em>{meta}</em>
      </div>
      <div className="shell-window-actions" onPointerDown={(event) => event.stopPropagation()}>
        {actions}
      </div>
    </div>
  );
}

function FloatingWindowFlight({ flight, frameRef, icon, style }: FloatingWindowFlightProps) {
  return (
    <div ref={frameRef} className={`shell-flight shell-flight-${flight.direction}`} style={style}>
      <div className="shell-flight-header">
        {icon}
        <span>{flight.containerName}</span>
        <em>{flight.meta}</em>
      </div>
      <div className="shell-flight-body" />
    </div>
  );
}

function MinimizedWindowButton({ ariaLabel, className, icon, onClick }: MinimizedWindowButtonProps) {
  return (
    <button className={`shell-minimized-button${className ? ` ${className}` : ""}`} type="button" onClick={onClick} aria-label={ariaLabel}>
      {icon}
    </button>
  );
}

function buildWindowClassName(className: string | undefined, dockState: ShellDockState, isMaximized: boolean) {
  return [
    "shell-window",
    className,
    dockState === "minimized" ? "shell-window-hidden" : null,
    isMaximized ? "shell-window-maximized" : null,
  ].filter(Boolean).join(" ");
}

function beginWindowDrag(
  rect: ShellRect,
  event: ReactPointerEvent<HTMLDivElement>,
  options: { capturePointer?: boolean } = {},
): WindowDragState {
  if (options.capturePointer) event.currentTarget.setPointerCapture(event.pointerId);
  return { x: rect.x, y: rect.y, startX: event.clientX, startY: event.clientY };
}

function beginWindowResize(
  target: WindowResizeState["target"],
  rect: ShellRect,
  event: ReactPointerEvent<HTMLDivElement>,
): WindowResizeState {
  event.currentTarget.setPointerCapture(event.pointerId);
  return { target, width: rect.width, height: rect.height, startX: event.clientX, startY: event.clientY };
}

function getDraggedWindowPosition(drag: WindowDragState, event: PointerEvent) {
  return {
    x: clamp(drag.x + event.clientX - drag.startX, 8, window.innerWidth - 80),
    y: clamp(drag.y + event.clientY - drag.startY, 8, window.innerHeight - 80),
  };
}

function getResizedWindowSize(resize: WindowResizeState, event: PointerEvent): Pick<ShellRect, "width" | "height"> {
  return {
    width: clamp(resize.width + event.clientX - resize.startX, MIN_WIDTH, window.innerWidth - 24),
    height: clamp(resize.height + event.clientY - resize.startY, MIN_HEIGHT, window.innerHeight - 24),
  };
}

function clampWindowToViewport<T extends ShellRect>(rect: T): T {
  return {
    ...rect,
    x: clamp(rect.x, 8, window.innerWidth - 80),
    y: clamp(rect.y, 8, window.innerHeight - 80),
    width: clamp(rect.width, MIN_WIDTH, window.innerWidth - 24),
    height: clamp(rect.height, MIN_HEIGHT, window.innerHeight - 24),
  };
}

function clamp(value: number, min: number, max: number) {
  return Math.max(min, Math.min(value, max));
}

function buildWindowStyle(rect: ShellRect) {
  return {
    left: rect.x,
    top: rect.y,
    width: rect.width,
    height: rect.height,
  } satisfies CSSProperties;
}

function getInitialNoVNCRect(): ShellRect {
  const width = Math.min(NOVNC_DEFAULT_WIDTH, Math.max(420, window.innerWidth - 24));
  const height = Math.min(NOVNC_DEFAULT_HEIGHT, Math.max(300, window.innerHeight - 24));
  return {
    x: Math.max(12, Math.round((window.innerWidth - width) / 2)),
    y: Math.max(72, Math.round((window.innerHeight - height) / 2)),
    width,
    height,
  };
}

function getInitialFileManagerRect(): ShellRect {
  const width = Math.min(760, Math.max(420, window.innerWidth - 24));
  const height = Math.min(520, Math.max(300, window.innerHeight - 24));
  return {
    x: Math.max(24, window.innerWidth - width - 36),
    y: Math.max(92, window.innerHeight - height - 36),
    width,
    height,
  };
}

function getShellRect(shell: ShellWindowState): ShellRect {
  return getWindowRect(shell);
}

function getWindowRect(rect: ShellRect): ShellRect {
  return {
    x: rect.x,
    y: rect.y,
    width: rect.width,
    height: rect.height,
  };
}

function getMaximizedShellRect(): ShellRect {
  return {
    x: MAXIMIZED_MARGIN,
    y: MAXIMIZED_MARGIN,
    width: Math.max(MIN_WIDTH, window.innerWidth - (MAXIMIZED_MARGIN * 2)),
    height: Math.max(MIN_HEIGHT, window.innerHeight - (MAXIMIZED_MARGIN * 2)),
  };
}

function buildShellFlight(shell: ShellWindowState, direction: ShellFlightState["direction"]): ShellFlightState {
  const shellRect = getShellRect(shell);
  const dockRect = getDockRect("shell");
  return {
    direction,
    containerName: shell.containerName,
    meta: shell.status,
    from: direction === "minimize" ? shellRect : dockRect,
    to: direction === "minimize" ? dockRect : shellRect,
  };
}

function buildNoVNCFlight(noVNC: NoVNCWindowState, direction: ShellFlightState["direction"]): ShellFlightState {
  const noVNCRect = getWindowRect(noVNC);
  const dockRect = getDockRect("novnc");
  return {
    direction,
    containerName: noVNC.containerName,
    meta: "screen",
    from: direction === "minimize" ? noVNCRect : dockRect,
    to: direction === "minimize" ? dockRect : noVNCRect,
  };
}

function buildFileManagerFlight(fm: FileManagerWindowState, direction: ShellFlightState["direction"]): ShellFlightState {
  const fmRect = getWindowRect(fm);
  const dockRect = getDockRect("filemanager");
  return {
    direction,
    containerName: fm.containerName,
    meta: "files",
    from: direction === "minimize" ? fmRect : dockRect,
    to: direction === "minimize" ? dockRect : fmRect,
  };
}

function buildWindowFlightStyle(flight: ShellFlightState) {
  const base = getFlightBaseRect(flight);
  return {
    left: base.x,
    top: base.y,
    width: base.width,
    height: base.height,
    opacity: getFlightOpacity(flight.direction, 0),
    transform: buildWindowFlightTransform(base, flight.from),
  } satisfies CSSProperties;
}

function getDockRect(slot: FloatingWindowDockSlot): ShellRect {
  let yOffset = 0;
  if (slot === "novnc") yOffset = DOCK_BUTTON_GAP;
  else if (slot === "filemanager") yOffset = DOCK_BUTTON_GAP * 2;
  return {
    x: window.innerWidth - DOCK_BUTTON_RIGHT - DOCK_BUTTON_SIZE,
    y: (window.innerHeight / 2) + yOffset,
    width: DOCK_BUTTON_SIZE,
    height: DOCK_BUTTON_SIZE,
  };
}

function closeSocket(socket: WebSocket | null) {
  if (!socket || socket.readyState === WebSocket.CLOSED || socket.readyState === WebSocket.CLOSING) return;
  socket.close();
}

function isSocketActive(socket: WebSocket | null) {
  return socket !== null && socket.readyState !== WebSocket.CLOSED && socket.readyState !== WebSocket.CLOSING;
}

function cancelFlightFrame(frameRef: MutableRefObject<number | null>) {
  if (frameRef.current === null) return;
  window.cancelAnimationFrame(frameRef.current);
  frameRef.current = null;
}

function animateWindowFlight(
  element: HTMLDivElement,
  flight: ShellFlightState,
  frameRef: MutableRefObject<number | null>,
  onDone: () => void,
) {
  const startedAt = performance.now();
  const base = getFlightBaseRect(flight);

  const tick = (now: number) => {
    const progress = clamp((now - startedAt) / WINDOW_DOCK_TRANSITION_MS, 0, 1);
    const eased = easeInOutCubic(progress);
    const rect = interpolateRect(flight.from, flight.to, eased);

    element.style.opacity = String(getFlightOpacity(flight.direction, eased));
    element.style.transform = buildWindowFlightTransform(base, rect);

    if (progress < 1) {
      frameRef.current = window.requestAnimationFrame(tick);
      return;
    }

    frameRef.current = null;
    onDone();
  };

  frameRef.current = window.requestAnimationFrame(tick);
  return () => cancelFlightFrame(frameRef);
}

function getFlightBaseRect(flight: ShellFlightState) {
  return flight.direction === "restore" ? flight.to : flight.from;
}

function buildWindowFlightTransform(base: ShellRect, rect: ShellRect) {
  const scaleX = rect.width / base.width;
  const scaleY = rect.height / base.height;
  const translateX = rect.x - base.x;
  const translateY = rect.y - base.y;
  return `matrix(${scaleX}, 0, 0, ${scaleY}, ${translateX}, ${translateY})`;
}

function interpolateRect(from: ShellRect, to: ShellRect, progress: number): ShellRect {
  return {
    x: lerp(from.x, to.x, progress),
    y: lerp(from.y, to.y, progress),
    width: lerp(from.width, to.width, progress),
    height: lerp(from.height, to.height, progress),
  };
}

function lerp(from: number, to: number, progress: number) {
  return from + ((to - from) * progress);
}

function easeInOutCubic(progress: number) {
  return progress < 0.5
    ? 4 * progress * progress * progress
    : 1 - (Math.pow(-2 * progress + 2, 3) / 2);
}

function getFlightOpacity(direction: ShellFlightState["direction"], progress: number) {
  return direction === "minimize" ? 1 - (0.78 * progress) : 0.22 + (0.78 * progress);
}

function snapShellHeightToRows(
  host: HTMLDivElement,
  terminal: Terminal,
  setShell: Dispatch<SetStateAction<ShellWindowState | null>>,
) {
  const cellHeight = getTerminalCellHeight(terminal);
  if (!cellHeight || !terminal.element) return;

  const terminalStyle = window.getComputedStyle(terminal.element);
  const terminalPaddingY = cssNumber(terminalStyle, "padding-top") + cssNumber(terminalStyle, "padding-bottom");
  const visibleHostHeight = host.getBoundingClientRect().height;
  const targetHostHeight = Math.ceil((terminal.rows * cellHeight) + terminalPaddingY);
  const delta = targetHostHeight - visibleHostHeight;
  if (Math.abs(delta) < 1) return;

  setShell((current) => current && !current.isMaximized ? {
    ...current,
    height: clamp(current.height + delta, MIN_HEIGHT, window.innerHeight - 24),
  } : current);
}

function getTerminalCellHeight(terminal: Terminal) {
  const dimensions = (terminal as unknown as {
    _core?: { _renderService?: { dimensions?: { css?: { cell?: { height?: number } } } } };
  })._core?._renderService?.dimensions;
  const height = dimensions?.css?.cell?.height;
  return typeof height === "number" && Number.isFinite(height) && height > 0 ? height : null;
}

function cssNumber(style: CSSStyleDeclaration, property: string) {
  const value = Number.parseFloat(style.getPropertyValue(property));
  return Number.isFinite(value) ? value : 0;
}
