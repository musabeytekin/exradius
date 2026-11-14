import { cn } from "./cn";

export type Variant = "primary" | "secondary" | "ghost" | "danger";

const variants: Record<Variant, string> = {
  primary: "bg-accent text-accent-fg hover:bg-accent-hover shadow-[inset_0_1px_0_rgb(255_255_255/0.12)]",
  secondary: "bg-surface text-fg border border-border hover:border-border-strong hover:bg-surface-2",
  ghost: "text-muted hover:text-fg hover:bg-surface-2",
  danger: "bg-surface text-err border border-border hover:border-err hover:bg-err-soft",
};

export function buttonClass(variant: Variant = "secondary", small = false) {
  return cn(
    "inline-flex shrink-0 select-none items-center justify-center gap-1.5 rounded-md font-medium whitespace-nowrap",
    "transition-colors duration-100 disabled:pointer-events-none disabled:opacity-50",
    small ? "h-7 px-2.5 text-sm" : "h-9 px-3 text-sm sm:h-8",
    variants[variant],
  );
}

export const inputClass =
  "w-full rounded-md border border-border bg-surface px-2.5 py-1.5 text-base text-fg placeholder:text-subtle " +
  "hover:border-border-strong focus:border-accent focus:outline-none sm:text-sm";
