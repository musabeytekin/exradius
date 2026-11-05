import { useSyncExternalStore } from "react";

export interface ToastItem {
  id: number;
  title: string;
  description?: string;
  tone: "ok" | "err" | "info";
  action?: { label: string; onClick: () => void };
  duration: number;
}

let items: ToastItem[] = [];
let next = 1;
const listeners = new Set<() => void>();
const emit = () => listeners.forEach((l) => l());

export function toast(t: Omit<ToastItem, "id" | "tone" | "duration"> & Partial<Pick<ToastItem, "tone" | "duration">>) {
  const item: ToastItem = { id: next++, tone: "info", duration: t.action ? 7000 : 4000, ...t };
  items = [...items.slice(-3), item];
  emit();
  return item.id;
}
toast.ok = (title: string, description?: string) => toast({ title, description, tone: "ok" });
toast.err = (title: string, error?: unknown) =>
  toast({ title, description: error instanceof Error ? error.message : error ? String(error) : undefined, tone: "err", duration: 7000 });

export function dismissToast(id: number) {
  items = items.filter((i) => i.id !== id);
  emit();
}

export function useToasts() {
  return useSyncExternalStore(
    (cb) => {
      listeners.add(cb);
      return () => listeners.delete(cb);
    },
    () => items,
  );
}
