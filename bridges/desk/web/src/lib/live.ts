import { useQueryClient } from "@tanstack/react-query";
import { useEffect } from "react";

/** Desk's stream (design §9): a changed task's key, then the browser reads it again. */
export function useLive(enabled: boolean) {
  const qc = useQueryClient();
  useEffect(() => {
    if (!enabled || typeof EventSource === "undefined") return;
    const source = new EventSource("/api/stream");
    let broken = false;
    source.addEventListener("task", (e) => {
      const { key } = JSON.parse((e as MessageEvent).data) as { key: string };
      qc.invalidateQueries({ queryKey: ["task", key] });
      qc.invalidateQueries({ queryKey: ["tasks"] });
    });
    source.onerror = () => {
      broken = true;
    };
    source.onopen = () => {
      if (broken) qc.invalidateQueries(); // missed changes while disconnected
      broken = false;
    };
    return () => source.close();
  }, [enabled, qc]);
}
