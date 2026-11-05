import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useState } from "react";
import { api } from "./api";
import { qk } from "./queries";
import type { Envelope, StoredEvent, TranscriptChunk } from "./types";

export type FeedStatus = "idle" | "connecting" | "live" | "ended" | "reconnecting";

export interface FeedEvent {
  id: string;
  type: Envelope["type"];
  envelope: Envelope;
}

export interface TaskFeed {
  chunks: TranscriptChunk[];
  events: FeedEvent[];
  status: FeedStatus;
  /** True while the transcript is still being loaded the first time. */
  loading: boolean;
}

async function loadTranscript(taskId: string): Promise<TranscriptChunk[]> {
  const all: TranscriptChunk[] = [];
  let after = 0;
  for (;;) {
    const page = await api.tasks.transcript(taskId, after, 5000);
    all.push(...page);
    if (page.length < 5000) return all;
    after = page[page.length - 1].seq;
  }
}

const fromStored = (e: StoredEvent): FeedEvent => ({ id: e.id, type: e.type, envelope: e.payload });

/**
 * Transcript and events of one task. A task that is still going (or not stored yet,
 * because it was just sent) is followed over the server-sent event stream, which replays
 * everything and then goes live. A task that had ended when the page opened is loaded
 * once over REST. The mode is fixed at first load so the view never flips mid-way.
 */
export function useTaskFeed(taskId: string, terminalAtLoad: boolean | undefined): TaskFeed {
  const qc = useQueryClient();
  const [mode, setMode] = useState<"rest" | "stream" | null>(null);
  if (mode === null && terminalAtLoad !== undefined) setMode(terminalAtLoad ? "rest" : "stream");
  const useRest = mode === "rest";

  const transcript = useQuery({
    queryKey: qk.transcript(taskId),
    queryFn: () => loadTranscript(taskId),
    enabled: useRest,
    staleTime: Infinity,
    retry: false,
  });
  const storedEvents = useQuery({
    queryKey: [...qk.task(taskId), "events"],
    queryFn: () => api.tasks.events(taskId),
    enabled: useRest,
    staleTime: Infinity,
  });

  const [stream, setStream] = useState<{ chunks: TranscriptChunk[]; events: FeedEvent[]; status: FeedStatus }>({ chunks: [], events: [], status: "idle" });

  useEffect(() => {
    if (mode !== "stream") return;
    const chunks = new Map<string, TranscriptChunk>();
    const events = new Map<string, FeedEvent>();
    let status: FeedStatus = "connecting";
    let frame = 0;
    let invalidateTimer: number | undefined;
    const flush = () => {
      frame = 0;
      setStream({
        chunks: [...chunks.values()].sort((a, b) => a.seq - b.seq),
        events: [...events.values()],
        status,
      });
    };
    const schedule = () => {
      if (!frame) frame = requestAnimationFrame(flush);
    };
    const refreshTask = () => {
      window.clearTimeout(invalidateTimer);
      invalidateTimer = window.setTimeout(() => {
        qc.invalidateQueries({ queryKey: qk.task(taskId), exact: true });
        qc.invalidateQueries({ queryKey: qk.allTasks });
      }, 250);
    };

    const es = new EventSource(api.tasks.streamUrl(taskId), { withCredentials: true });
    es.onopen = () => {
      status = "live";
      schedule();
    };
    es.onerror = () => {
      if (status !== "ended") {
        status = es.readyState === EventSource.CLOSED ? "ended" : "reconnecting";
        schedule();
      }
    };
    es.addEventListener("transcript", (e) => {
      const data = JSON.parse((e as MessageEvent).data) as { run_id: string; seq: number; payload: TranscriptChunk["payload"] };
      chunks.set(`${data.run_id}:${data.seq}`, { seq: data.seq, payload: data.payload });
      schedule();
    });
    for (const type of ["status", "info_request", "result"] as const) {
      es.addEventListener(type, (e) => {
        const me = e as MessageEvent;
        const envelope = JSON.parse(me.data) as Envelope;
        events.set(me.lastEventId || `${type}:${events.size}`, { id: me.lastEventId, type, envelope });
        refreshTask();
        const state = (envelope.payload as { state?: string }).state;
        // The server closes after the result, or after a rejection of a task it never stored.
        // Close here too, or EventSource would reconnect and replay.
        if (type === "result" || (type === "status" && state === "rejected")) {
          status = "ended";
          es.close();
        }
        schedule();
      });
    }
    return () => {
      es.close();
      cancelAnimationFrame(frame);
      window.clearTimeout(invalidateTimer);
    };
  }, [taskId, qc, mode]);

  if (useRest) {
    return {
      chunks: transcript.data ?? [],
      events: (storedEvents.data ?? []).map(fromStored),
      status: "ended",
      loading: transcript.isLoading,
    };
  }
  return { ...stream, loading: mode === null || ((stream.status === "idle" || stream.status === "connecting") && stream.chunks.length === 0 && stream.events.length === 0) };
}
