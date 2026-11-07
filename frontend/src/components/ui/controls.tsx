import * as CheckboxPrimitive from "@radix-ui/react-checkbox";
import * as Select from "@radix-ui/react-select";
import * as TabsPrimitive from "@radix-ui/react-tabs";
import * as ToggleGroup from "@radix-ui/react-toggle-group";
import { useMemo, type ReactNode } from "react";
import { cn } from "@/lib/cn";
import { Icon, type IconName } from "./icons";

export interface PickOption {
  value: string;
  label: ReactNode;
  /** Text used for the trigger when label is rich. */
  text?: string;
  description?: ReactNode;
  meta?: ReactNode;
  disabled?: boolean;
  group?: string;
}

const NONE = "__none__";
const ACTION_PREFIX = "__action__:";

/**
 * Pick list on Radix Select. Groups render in order of first appearance. `actions` render
 * at the bottom (for inline "create a secret") and never become the value.
 */
export function PickList({
  value, onChange, options, placeholder = "Choose…", noneLabel, actions, invalid, disabled, className, id, mono, size = "md", "aria-label": ariaLabel,
}: {
  value: string | null; onChange: (v: string | null) => void; options: PickOption[];
  placeholder?: string; noneLabel?: string; actions?: { key: string; label: ReactNode; icon?: IconName; onSelect: () => void }[];
  invalid?: boolean; disabled?: boolean; className?: string; id?: string; mono?: boolean; size?: "sm" | "md"; "aria-label"?: string;
}) {
  const groups = useMemo(() => {
    const order: string[] = [];
    const map = new Map<string, PickOption[]>();
    for (const o of options) {
      const g = o.group ?? "";
      if (!map.has(g)) {
        map.set(g, []);
        order.push(g);
      }
      map.get(g)!.push(o);
    }
    return order.map((g) => ({ group: g, options: map.get(g)! }));
  }, [options]);
  const selected = options.find((o) => o.value === value);

  return (
    <Select.Root
      value={value ?? (noneLabel ? NONE : undefined)}
      disabled={disabled}
      onValueChange={(v) => {
        if (v.startsWith(ACTION_PREFIX)) {
          actions?.find((a) => ACTION_PREFIX + a.key === v)?.onSelect();
          return;
        }
        onChange(v === NONE ? null : v);
      }}
    >
      <Select.Trigger
        id={id}
        aria-label={ariaLabel}
        aria-invalid={invalid || undefined}
        className={cn(
          "group inline-flex w-full items-center justify-between gap-2 rounded-md border bg-surface px-2.5 text-left text-sm text-fg",
          "transition-colors hover:border-border-strong focus:outline-none focus:border-accent focus:ring-3 focus:ring-[var(--ring)]",
          "data-[placeholder]:text-subtle disabled:opacity-60",
          size === "sm" ? "h-7" : "h-8",
          invalid ? "border-err" : "border-border",
          className,
        )}
      >
        <span className={cn("min-w-0 truncate", mono && "font-mono text-[12.5px]")}>
          <Select.Value placeholder={placeholder}>
            {selected ? (selected.text ?? selected.label) : value === null && noneLabel ? noneLabel : undefined}
          </Select.Value>
        </span>
        <Select.Icon className="text-subtle group-hover:text-muted">
          <Icon name="chevronDown" size={14} />
        </Select.Icon>
      </Select.Trigger>
      <Select.Portal>
        <Select.Content
          position="popper"
          sideOffset={4}
          className="z-[60] max-h-[min(360px,var(--radix-select-content-available-height))] min-w-[var(--radix-select-trigger-width)] overflow-hidden rounded-lg border border-border bg-surface shadow-pop data-[state=open]:animate-pop-in"
        >
          <Select.Viewport className="p-1">
            {noneLabel && <Item value={NONE} label={<span className="text-muted">{noneLabel}</span>} />}
            {groups.map(({ group, options: opts }, gi) => (
              <Select.Group key={group || gi}>
                {group && <Select.Label className="px-2 pb-1 pt-2 text-2xs font-medium uppercase tracking-wide text-subtle">{group}</Select.Label>}
                {opts.map((o) => (
                  <Item key={o.value} value={o.value} label={o.label} description={o.description} meta={o.meta} disabled={o.disabled} mono={mono} />
                ))}
              </Select.Group>
            ))}
            {options.length === 0 && !noneLabel && <div className="px-2 py-2 text-sm text-subtle">Nothing to pick yet</div>}
            {actions && actions.length > 0 && (
              <>
                <Select.Separator className="my-1 h-px bg-border" />
                {actions.map((a) => (
                  <Select.Item
                    key={a.key}
                    value={ACTION_PREFIX + a.key}
                    className="flex h-8 cursor-default select-none items-center gap-2 rounded-md px-2 text-sm text-accent outline-none data-[highlighted]:bg-accent-soft"
                  >
                    <Icon name={a.icon ?? "plus"} size={14} />
                    <Select.ItemText>{a.label}</Select.ItemText>
                  </Select.Item>
                ))}
              </>
            )}
          </Select.Viewport>
        </Select.Content>
      </Select.Portal>
    </Select.Root>
  );
}

function Item({ value, label, description, meta, disabled, mono }: { value: string; label: ReactNode; description?: ReactNode; meta?: ReactNode; disabled?: boolean; mono?: boolean }) {
  return (
    <Select.Item
      value={value}
      disabled={disabled}
      className="relative flex min-h-8 cursor-default select-none items-center gap-2 rounded-md py-1.5 pl-7 pr-2 text-sm text-fg outline-none data-[disabled]:opacity-50 data-[highlighted]:bg-surface-2"
    >
      <Select.ItemIndicator className="absolute left-2 text-accent">
        <Icon name="check" size={14} strokeWidth={2.25} />
      </Select.ItemIndicator>
      <div className="min-w-0 flex-1">
        <Select.ItemText>
          <span className={cn(mono && "font-mono text-[12.5px]")}>{label}</span>
        </Select.ItemText>
        {description && <div className="truncate text-xs text-subtle">{description}</div>}
      </div>
      {meta && <span className="shrink-0 text-xs text-subtle">{meta}</span>}
    </Select.Item>
  );
}

export function Segmented<T extends string>({ value, onChange, options, size = "md", "aria-label": ariaLabel }: {
  value: T; onChange: (v: T) => void; options: { value: T; label: ReactNode; icon?: IconName }[]; size?: "sm" | "md"; "aria-label"?: string;
}) {
  return (
    <ToggleGroup.Root
      type="single"
      value={value}
      aria-label={ariaLabel}
      onValueChange={(v) => v && onChange(v as T)}
      className={cn("inline-flex rounded-md border border-border bg-surface-2 p-0.5", size === "sm" ? "h-7" : "h-8")}
    >
      {options.map((o) => (
        <ToggleGroup.Item
          key={o.value}
          value={o.value}
          className={cn(
            "inline-flex items-center gap-1.5 rounded-[5px] px-2.5 text-sm font-medium text-muted transition-colors",
            "hover:text-fg data-[state=on]:bg-surface data-[state=on]:text-fg data-[state=on]:shadow-[0_1px_2px_rgb(0_0_0/0.08)]",
            "focus-visible:outline-2",
          )}
        >
          {o.icon && <Icon name={o.icon} size={14} />}
          {o.label}
        </ToggleGroup.Item>
      ))}
    </ToggleGroup.Root>
  );
}

export function Checkbox({ checked, onChange, id, disabled, "aria-label": ariaLabel }: {
  checked: boolean; onChange: (v: boolean) => void; id?: string; disabled?: boolean; "aria-label"?: string;
}) {
  return (
    <CheckboxPrimitive.Root
      id={id}
      checked={checked}
      disabled={disabled}
      aria-label={ariaLabel}
      onCheckedChange={(v) => onChange(v === true)}
      className="flex size-4 shrink-0 items-center justify-center rounded-[4px] border border-border-strong bg-surface transition-colors data-[state=checked]:border-accent data-[state=checked]:bg-accent disabled:opacity-50"
    >
      <CheckboxPrimitive.Indicator className="text-accent-fg">
        <Icon name="check" size={12} strokeWidth={2.75} />
      </CheckboxPrimitive.Indicator>
    </CheckboxPrimitive.Root>
  );
}

export const Tabs = {
  Root: TabsPrimitive.Root,
  Content: ({ value, children, className }: { value: string; children: ReactNode; className?: string }) => (
    <TabsPrimitive.Content value={value} className={cn("focus:outline-none", className)}>{children}</TabsPrimitive.Content>
  ),
  List: ({ children, className }: { children: ReactNode; className?: string }) => (
    <TabsPrimitive.List className={cn("flex items-center gap-1 border-b border-border", className)}>{children}</TabsPrimitive.List>
  ),
  Trigger: ({ value, children, count, attention }: { value: string; children: ReactNode; count?: number; attention?: boolean }) => (
    <TabsPrimitive.Trigger
      value={value}
      className={cn(
        "relative -mb-px inline-flex h-9 items-center gap-1.5 border-b-2 border-transparent px-2.5 text-sm font-medium text-muted transition-colors",
        "hover:text-fg data-[state=active]:border-accent data-[state=active]:text-fg",
      )}
    >
      {children}
      {count !== undefined && (
        <span className="rounded-full bg-surface-2 px-1.5 text-2xs tabular text-muted">{count}</span>
      )}
      {attention && <span className="size-1.5 rounded-full bg-warn" />}
    </TabsPrimitive.Trigger>
  ),
};

export const Table = {
  Root: ({ children, className }: { children: ReactNode; className?: string }) => (
    <div className={cn("overflow-x-auto", className)}>
      <table className="w-full border-collapse text-sm">{children}</table>
    </div>
  ),
  Head: ({ children }: { children: ReactNode }) => (
    <thead className="border-b border-border text-left text-2xs font-medium uppercase tracking-wide text-subtle">{children}</thead>
  ),
  Th: ({ children, className }: { children?: ReactNode; className?: string }) => (
    <th className={cn("h-8 px-4 font-medium first:pl-4 last:pr-4", className)}>{children}</th>
  ),
  Row: ({ children, onClick, className, selected }: { children: ReactNode; onClick?: () => void; className?: string; selected?: boolean }) => (
    <tr
      onClick={onClick}
      className={cn(
        "border-b border-border last:border-b-0 transition-colors",
        onClick && "cursor-pointer hover:bg-surface-2/70",
        selected && "bg-accent-soft/50",
        className,
      )}
    >
      {children}
    </tr>
  ),
  Td: ({ children, className, colSpan }: { children?: ReactNode; className?: string; colSpan?: number }) => (
    <td colSpan={colSpan} className={cn("h-11 px-4 align-middle", className)}>{children}</td>
  ),
};
