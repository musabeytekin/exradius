import type { ButtonHTMLAttributes, ReactNode, SelectHTMLAttributes } from "react";
import { cn } from "@/lib/cn";
import { buttonClass, type Variant } from "@/lib/styles";

export function Button({ variant = "secondary", small, className, ...rest }:
  ButtonHTMLAttributes<HTMLButtonElement> & { variant?: Variant; small?: boolean }) {
  return <button type="button" className={cn(buttonClass(variant, small), className)} {...rest} />;
}

export function Select({ className, ...rest }: SelectHTMLAttributes<HTMLSelectElement>) {
  return (
    <select
      className={cn(
        "h-9 rounded-md border border-border bg-surface px-2 text-sm text-fg sm:h-8",
        "hover:border-border-strong disabled:opacity-60",
        className,
      )}
      {...rest}
    />
  );
}

type Tone = "info" | "warn" | "ok" | "err" | "muted" | "accent";

const tones: Record<Tone, string> = {
  info: "bg-info-soft text-info",
  warn: "bg-warn-soft text-warn",
  ok: "bg-ok-soft text-ok",
  err: "bg-err-soft text-err",
  muted: "bg-surface-2 text-muted",
  accent: "bg-accent-soft text-accent",
};

export function Chip({ tone = "muted", pulse, children, className }:
  { tone?: Tone; pulse?: boolean; children: ReactNode; className?: string }) {
  return (
    <span className={cn("inline-flex h-5 items-center gap-1.5 rounded px-1.5 text-xs font-medium", tones[tone], className)}>
      {pulse && <span className="h-1.5 w-1.5 animate-pulse-dot rounded-full bg-current" />}
      {children}
    </span>
  );
}

export function Avatar({ name, exradius }: { name: string; exradius?: boolean }) {
  const initials = name.split(/\s+/).filter(Boolean).slice(0, 2).map((w) => w[0]!.toUpperCase()).join("");
  return (
    <span
      aria-hidden
      className={cn(
        "inline-flex h-6 w-6 shrink-0 items-center justify-center rounded-full text-[10px] font-semibold",
        exradius ? "bg-accent text-accent-fg" : "bg-surface-3 text-muted",
      )}
    >
      {exradius ? "Ex" : initials || "?"}
    </span>
  );
}

export function Problem({ children }: { children: ReactNode }) {
  return <p role="alert" className="rounded-md bg-err-soft px-3 py-2 text-sm text-err">{children}</p>;
}

export function Loading() {
  return (
    <div className="space-y-2" aria-busy>
      {[0, 1, 2].map((i) => <div key={i} className="skeleton h-10 rounded-md" />)}
    </div>
  );
}
