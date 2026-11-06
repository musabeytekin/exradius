import { useQueryClient } from "@tanstack/react-query";
import { useRouter, type ErrorComponentProps } from "@tanstack/react-router";
import { useEffect, useRef, useState, useSyncExternalStore } from "react";
import { isUnreachable, reachability } from "@/lib/api";
import { Icon } from "./ui/icons";
import { Button, Card } from "./ui/primitives";

const RETRY_SECONDS = 5;

/** Counts down and calls `retry` every few seconds while mounted. */
function useAutoRetry(retry: () => void) {
  const [left, setLeft] = useState(RETRY_SECONDS);
  const retryRef = useRef(retry);
  useEffect(() => {
    retryRef.current = retry;
  }, [retry]);
  useEffect(() => {
    let n = RETRY_SECONDS;
    const t = window.setInterval(() => {
      n -= 1;
      if (n <= 0) {
        n = RETRY_SECONDS;
        retryRef.current();
      }
      setLeft(n);
    }, 1000);
    return () => window.clearInterval(t);
  }, []);
  return left;
}

function UnreachableCard({ onRetry }: { onRetry: () => void }) {
  const left = useAutoRetry(onRetry);
  return (
    <div className="flex min-h-[60vh] items-center justify-center p-6">
      <Card className="w-[min(440px,100%)] p-6 text-center">
        <div className="mx-auto mb-3 flex size-10 items-center justify-center rounded-full bg-warn-soft text-warn">
          <Icon name="alert" size={18} />
        </div>
        <h1 className="text-[15px] font-semibold text-fg">Exradius API is not reachable</h1>
        <p className="mt-1.5 text-sm text-muted">
          The panel is up, but the API behind it did not answer. It may be restarting, which usually takes under a minute.
        </p>
        <div className="mt-5 flex items-center justify-center gap-3">
          <Button variant="primary" icon="refresh" onClick={onRetry}>Retry now</Button>
          <span className="text-xs tabular text-subtle">Retrying in {left} s</span>
        </div>
      </Card>
    </div>
  );
}

/** The router's error screen: the unreachable card, or the error itself for anything else. */
export function RouteError({ error, reset }: ErrorComponentProps) {
  const router = useRouter();
  const retry = () => {
    reset();
    void router.invalidate();
  };
  if (isUnreachable(error)) return <UnreachableCard onRetry={retry} />;
  return (
    <div className="flex min-h-[60vh] items-center justify-center p-6">
      <Card className="w-[min(520px,100%)] p-6">
        <h1 className="text-[15px] font-semibold text-fg">Something went wrong</h1>
        <p className="mt-1.5 break-words font-mono text-xs text-muted">{error instanceof Error ? error.message : String(error)}</p>
        <Button className="mt-4" variant="secondary" icon="refresh" onClick={retry}>Try again</Button>
      </Card>
    </div>
  );
}

/** A strip at the top of the shell while requests fail to reach the API; refetches on retry. */
export function UnreachableBanner() {
  const reachable = useSyncExternalStore(reachability.subscribe, reachability.get);
  if (reachable) return null;
  return <BannerBody />;
}

function BannerBody() {
  const qc = useQueryClient();
  const retry = () => void qc.refetchQueries({ type: "active" });
  const left = useAutoRetry(retry);
  return (
    <div role="alert" className="sticky top-0 z-30 flex items-center gap-3 border-b border-warn/25 bg-warn-soft px-8 py-2 text-sm">
      <Icon name="alert" size={15} className="text-warn" />
      <span className="font-medium text-fg">Exradius API is not reachable.</span>
      <span className="text-muted">Showing what was last loaded. Retrying in {left} s.</span>
      <Button size="sm" variant="secondary" icon="refresh" className="ml-auto" onClick={retry}>Retry</Button>
    </div>
  );
}
