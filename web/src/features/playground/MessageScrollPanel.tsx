import { Button, Spin } from "@douyinfe/semi-ui";
import { ArrowDown } from "lucide-react";
import { ReactNode, RefObject, useCallback, useEffect, useLayoutEffect, useRef } from "react";
import { useAutoFollowScroll } from "./useAutoFollowScroll";

type MessageScrollPanelProps = {
  ariaLabel: string;
  children: (tailRef: RefObject<HTMLDivElement | null>) => ReactNode;
  className?: string;
  contentClassName?: string;
  enabled?: boolean;
  loading?: boolean;
  loadingPrevious?: boolean;
  onLoadPrevious?: () => void;
  preserveScrollKey?: string | number | null;
  resetKey?: string | number | null;
  scrollButtonClassName?: string;
  watch?: readonly unknown[];
};

export function MessageScrollPanel({
  ariaLabel,
  children,
  className = "",
  contentClassName = "",
  enabled = true,
  loading = false,
  loadingPrevious = false,
  onLoadPrevious,
  preserveScrollKey,
  resetKey,
  scrollButtonClassName = "",
  watch = [],
}: MessageScrollPanelProps) {
  const containerRef = useRef<HTMLDivElement | null>(null);
  const previousHeightRef = useRef(0);
  const loadPreviousThrottleRef = useRef(false);

  useEffect(() => {
    if (!loadingPrevious) loadPreviousThrottleRef.current = false;
  }, [loadingPrevious]);

  const onScrollToTop = useCallback(() => {
    const container = containerRef.current;
    if (!container || !onLoadPrevious || loading || loadingPrevious || loadPreviousThrottleRef.current) return;
    loadPreviousThrottleRef.current = true;
    previousHeightRef.current = container.scrollHeight;
    onLoadPrevious();
  }, [loading, loadingPrevious, onLoadPrevious]);

  const {
    following,
    tailRef,
    scrollHandlers,
    scrollToLatest,
  } = useAutoFollowScroll({
    enabled,
    containerRef,
    resetKey,
    watch,
    suspendAutoFollow: Boolean(previousHeightRef.current) || loadingPrevious,
    onScrollToTop,
  });

  useLayoutEffect(() => {
    const container = containerRef.current;
    const previousHeight = previousHeightRef.current;
    if (!container || !previousHeight) return;
    const nextScrollTop = container.scrollTop + container.scrollHeight - previousHeight;
    container.style.overflowAnchor = "none";
    container.scrollTop = nextScrollTop;
    previousHeightRef.current = 0;
    window.requestAnimationFrame(() => {
      if (containerRef.current === container) container.style.overflowAnchor = "";
    });
  }, [preserveScrollKey]);

  return (
    <div className={`message-scroll-shell${className ? ` ${className}` : ""}`}>
      <div
        ref={containerRef}
        className="message-scroll-viewport"
        aria-label={ariaLabel}
        aria-busy={loading}
        {...scrollHandlers}
      >
        <div className={`message-scroll-content${contentClassName ? ` ${contentClassName}` : ""}`}>
          {children(tailRef)}
        </div>
      </div>
      {loading ? (
        <div className="message-scroll-loading" aria-hidden="true">
          <Spin spinning />
        </div>
      ) : null}
      {enabled && !following ? (
        <Button
          className={`message-scroll-tail-floating${scrollButtonClassName ? ` ${scrollButtonClassName}` : ""}`}
          icon={<ArrowDown size={16} />}
          theme="solid"
          type="tertiary"
          onClick={scrollToLatest}
          aria-label="Scroll to latest message"
        />
      ) : null}
    </div>
  );
}
