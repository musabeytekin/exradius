import { forwardRef, type ButtonHTMLAttributes, type InputHTMLAttributes, type ReactNode, type TextareaHTMLAttributes } from "react";
import { cn } from "@/lib/cn";
import { buttonClass, type ButtonSize as Size, type ButtonVariant as Variant } from "@/lib/buttonStyles";
import { Icon, type IconName } from "./icons";

export interface ButtonProps extends ButtonHTMLAttributes<HTMLButtonElement> {
  variant?: Variant;
  size?: Size;
  icon?: IconName;
  iconRight?: IconName;
  loading?: boolean;
  /** Square, icon-only button; give it an aria-label. */
  square?: boolean;
}

export const Button = forwardRef<HTMLButtonElement, ButtonProps>(function Button(
  { variant = "secondary", size = "md", icon, iconRight, loading, square, className, children, disabled, type = "button", ...rest },
  ref,
) {
  return (
    <button
      ref={ref}
      type={type}
      disabled={disabled || loading}
      className={cn(
        buttonClass(variant, size, square),
        className,
      )}
      {...rest}
    >
      {loading ? <Spinner size={size === "md" ? 14 : 12} /> : icon ? <Icon name={icon} size={size === "md" ? 15 : 14} /> : null}
      {children}
      {iconRight && <Icon name={iconRight} size={14} className="opacity-70" />}
    </button>
  );
});

export function Spinner({ size = 14, className }: { size?: number; className?: string }) {
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" className={cn("animate-spin", className)} aria-hidden="true">
      <circle cx="12" cy="12" r="9" fill="none" stroke="currentColor" strokeOpacity="0.25" strokeWidth="3" />
      <path d="M21 12a9 9 0 0 0-9-9" fill="none" stroke="currentColor" strokeWidth="3" strokeLinecap="round" />
    </svg>
  );
}

const fieldBase =
  "w-full rounded-md border bg-surface text-fg placeholder:text-subtle transition-[border-color,box-shadow] duration-100 " +
  "focus:outline-none focus:border-accent focus:ring-3 focus:ring-[var(--ring)] disabled:opacity-60 disabled:bg-surface-2";

export interface InputProps extends InputHTMLAttributes<HTMLInputElement> {
  invalid?: boolean;
  mono?: boolean;
  leading?: ReactNode;
  trailing?: ReactNode;
}

export const Input = forwardRef<HTMLInputElement, InputProps>(function Input(
  { invalid, mono, leading, trailing, className, ...rest },
  ref,
) {
  const input = (
    <input
      ref={ref}
      aria-invalid={invalid || undefined}
      className={cn(
        fieldBase,
        "h-8 px-2.5 text-sm",
        invalid ? "border-err focus:border-err focus:ring-[color-mix(in_oklab,var(--err)_30%,transparent)]" : "border-border hover:border-border-strong",
        mono && "font-mono text-[12.5px]",
        leading && "pl-8",
        trailing && "pr-9",
        !leading && !trailing && className,
      )}
      {...rest}
    />
  );
  if (!leading && !trailing) return input;
  return (
    <div className={cn("relative", className)}>
      {leading && <span className="pointer-events-none absolute inset-y-0 left-2.5 flex items-center text-subtle">{leading}</span>}
      {input}
      {trailing && <span className="absolute inset-y-0 right-1 flex items-center">{trailing}</span>}
    </div>
  );
});

export const Textarea = forwardRef<HTMLTextAreaElement, TextareaHTMLAttributes<HTMLTextAreaElement> & { invalid?: boolean; mono?: boolean }>(
  function Textarea({ invalid, mono, className, ...rest }, ref) {
    return (
      <textarea
        ref={ref}
        aria-invalid={invalid || undefined}
        className={cn(
          fieldBase,
          "px-2.5 py-2 text-sm leading-relaxed",
          invalid ? "border-err" : "border-border hover:border-border-strong",
          mono && "font-mono text-[12.5px] leading-[1.6]",
          className,
        )}
        {...rest}
      />
    );
  },
);

export function Field({
  label, hint, error, warning, children, className, htmlFor, aside, optional,
}: {
  label?: ReactNode; hint?: ReactNode; error?: ReactNode; warning?: ReactNode; children: ReactNode;
  className?: string; htmlFor?: string; aside?: ReactNode; optional?: boolean;
}) {
  return (
    <div className={cn("flex flex-col gap-1.5", className)}>
      {(label || aside) && (
        <div className="flex items-center justify-between gap-2">
          {label && (
            <label htmlFor={htmlFor} className="text-xs font-medium text-muted">
              {label}
              {optional && <span className="ml-1 font-normal text-subtle">optional</span>}
            </label>
          )}
          {aside}
        </div>
      )}
      {children}
      {error ? (
        <p className="flex items-start gap-1 text-xs text-err"><Icon name="alert" size={13} className="mt-0.5 shrink-0" />{error}</p>
      ) : warning ? (
        <p className="flex items-start gap-1 text-xs text-warn"><Icon name="alert" size={13} className="mt-0.5 shrink-0" />{warning}</p>
      ) : hint ? (
        <p className="text-xs text-subtle">{hint}</p>
      ) : null}
    </div>
  );
}

export function Kbd({ children, className }: { children: ReactNode; className?: string }) {
  return (
    <kbd className={cn("inline-flex h-[18px] min-w-[18px] items-center justify-center rounded border border-border bg-surface-2 px-1 font-sans text-[10.5px] font-medium text-muted", className)}>
      {children}
    </kbd>
  );
}

type Tone = "neutral" | "accent" | "ok" | "warn" | "err" | "info";
const tones: Record<Tone, string> = {
  neutral: "bg-surface-2 text-muted border-border",
  accent: "bg-accent-soft text-accent border-transparent",
  ok: "bg-ok-soft text-ok border-transparent",
  warn: "bg-warn-soft text-warn border-transparent",
  err: "bg-err-soft text-err border-transparent",
  info: "bg-info-soft text-info border-transparent",
};
const dotTones: Record<Tone, string> = {
  neutral: "bg-subtle", accent: "bg-accent", ok: "bg-ok", warn: "bg-warn", err: "bg-err", info: "bg-info",
};

export function Chip({ tone = "neutral", children, className, mono, dot, pulse, title }: {
  tone?: Tone; children: ReactNode; className?: string; mono?: boolean; dot?: boolean; pulse?: boolean; title?: string;
}) {
  return (
    <span
      title={title}
      className={cn(
        "inline-flex h-5 shrink-0 items-center gap-1.5 rounded-full border px-2 text-2xs font-medium whitespace-nowrap",
        tones[tone],
        mono && "font-mono text-[10.5px]",
        className,
      )}
    >
      {dot && <span className={cn("size-1.5 rounded-full", dotTones[tone], pulse && "animate-pulse-dot")} />}
      {children}
    </span>
  );
}

export function Dot({ tone = "neutral", pulse, className }: { tone?: Tone; pulse?: boolean; className?: string }) {
  return <span className={cn("inline-block size-2 shrink-0 rounded-full", dotTones[tone], pulse && "animate-pulse-dot", className)} />;
}

export type { Tone };

export function Skeleton({ className }: { className?: string }) {
  return <div className={cn("skeleton rounded-md", className)} />;
}

export function SkeletonRows({ rows = 4, className }: { rows?: number; className?: string }) {
  return (
    <div className={cn("flex flex-col gap-2 p-4", className)} aria-busy="true" aria-label="Loading">
      {Array.from({ length: rows }, (_, i) => (
        <Skeleton key={i} className="h-7" />
      ))}
    </div>
  );
}

export function EmptyState({ icon, title, children, action, className, compact }: {
  icon: IconName; title: ReactNode; children?: ReactNode; action?: ReactNode; className?: string; compact?: boolean;
}) {
  return (
    <div className={cn("flex flex-col items-center justify-center text-center", compact ? "gap-2 px-6 py-8" : "gap-3 px-8 py-14", className)}>
      <div className="relative flex size-11 items-center justify-center rounded-xl border border-border bg-surface-2 text-muted">
        <Icon name={icon} size={20} />
        <span className="absolute -inset-2 -z-10 rounded-2xl bg-[radial-gradient(closest-side,var(--accent-soft),transparent)]" />
      </div>
      <div className="flex max-w-md flex-col gap-1">
        <h3 className="text-sm font-semibold text-fg">{title}</h3>
        {children && <div className="text-sm text-muted">{children}</div>}
      </div>
      {action && <div className="mt-1 flex items-center gap-2">{action}</div>}
    </div>
  );
}

export function Card({ children, className, ...rest }: { children: ReactNode; className?: string } & React.HTMLAttributes<HTMLDivElement>) {
  return <div className={cn("rounded-lg border border-border bg-surface", className)} {...rest}>{children}</div>;
}

export function CardHeader({ title, description, actions, className }: { title: ReactNode; description?: ReactNode; actions?: ReactNode; className?: string }) {
  return (
    <div className={cn("flex items-start justify-between gap-4 border-b border-border px-4 py-3", className)}>
      <div className="min-w-0">
        <h2 className="text-sm font-semibold text-fg">{title}</h2>
        {description && <p className="mt-0.5 text-xs text-muted">{description}</p>}
      </div>
      {actions && <div className="flex shrink-0 items-center gap-2">{actions}</div>}
    </div>
  );
}

export function PageHeader({ title, description, actions, breadcrumb, meta }: {
  title: ReactNode; description?: ReactNode; actions?: ReactNode; breadcrumb?: ReactNode; meta?: ReactNode;
}) {
  return (
    <header className="flex flex-col gap-1 pb-6">
      {breadcrumb && <div className="mb-1 flex items-center gap-1.5 text-xs text-subtle">{breadcrumb}</div>}
      <div className="flex items-end justify-between gap-6">
        <div className="min-w-0">
          <h1 className="text-[19px] font-semibold tracking-[-0.01em] text-fg">{title}</h1>
          {description && <p className="mt-1 max-w-2xl text-sm text-muted">{description}</p>}
          {meta && <div className="mt-2 flex flex-wrap items-center gap-2">{meta}</div>}
        </div>
        {actions && <div className="flex shrink-0 items-center gap-2">{actions}</div>}
      </div>
    </header>
  );
}

export function Mono({ children, className, title }: { children: ReactNode; className?: string; title?: string }) {
  return <code title={title} className={cn("font-mono text-[12px] text-fg", className)}>{children}</code>;
}

export function Banner({ tone = "info", icon, title, children, action, className }: {
  tone?: Tone; icon?: IconName; title?: ReactNode; children?: ReactNode; action?: ReactNode; className?: string;
}) {
  const ring: Record<Tone, string> = {
    neutral: "border-border bg-surface-2", accent: "border-accent/25 bg-accent-soft",
    ok: "border-ok/25 bg-ok-soft", warn: "border-warn/25 bg-warn-soft", err: "border-err/25 bg-err-soft", info: "border-info/25 bg-info-soft",
  };
  const fg: Record<Tone, string> = { neutral: "text-muted", accent: "text-accent", ok: "text-ok", warn: "text-warn", err: "text-err", info: "text-info" };
  return (
    <div className={cn("flex items-start gap-3 rounded-lg border px-3.5 py-2.5", ring[tone], className)} role={tone === "err" ? "alert" : undefined}>
      <Icon name={icon ?? (tone === "err" || tone === "warn" ? "alert" : "info")} size={16} className={cn("mt-0.5 shrink-0", fg[tone])} />
      <div className="min-w-0 flex-1 text-sm">
        {title && <div className="font-medium text-fg">{title}</div>}
        {children && <div className="text-muted">{children}</div>}
      </div>
      {action && <div className="shrink-0">{action}</div>}
    </div>
  );
}
