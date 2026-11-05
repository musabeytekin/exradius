import * as AlertDialog from "@radix-ui/react-alert-dialog";
import * as DialogPrimitive from "@radix-ui/react-dialog";
import * as DropdownMenu from "@radix-ui/react-dropdown-menu";
import * as PopoverPrimitive from "@radix-ui/react-popover";
import * as ToastPrimitive from "@radix-ui/react-toast";
import * as TooltipPrimitive from "@radix-ui/react-tooltip";
import { useCallback, useRef, useState, type FormEvent, type ReactNode } from "react";
import { cn } from "@/lib/cn";
import { ConfirmContext, type ConfirmOptions } from "@/lib/confirm";
import { dismissToast, useToasts } from "@/lib/toast";
import { Icon } from "./icons";
import { Button, Input } from "./primitives";

const overlay = "fixed inset-0 z-40 bg-[rgb(10_11_14/0.45)] backdrop-blur-[2px] data-[state=open]:animate-fade-in";
const panel =
  "fixed left-1/2 top-[12vh] z-50 -translate-x-1/2 rounded-xl border border-border bg-surface shadow-pop " +
  "data-[state=open]:animate-pop-in focus:outline-none flex max-h-[80vh] flex-col";

export function Dialog({
  open, onOpenChange, title, description, children, footer, width = 520, onSubmit, trigger,
}: {
  open?: boolean; onOpenChange?: (o: boolean) => void; title: ReactNode; description?: ReactNode;
  children: ReactNode; footer?: ReactNode; width?: number; onSubmit?: (e: FormEvent) => void; trigger?: ReactNode;
}) {
  const body = (
    <>
      <div className="flex items-start justify-between gap-4 border-b border-border px-5 py-4">
        <div className="min-w-0">
          <DialogPrimitive.Title className="text-[15px] font-semibold text-fg">{title}</DialogPrimitive.Title>
          {description ? (
            <DialogPrimitive.Description className="mt-1 text-sm text-muted">{description}</DialogPrimitive.Description>
          ) : (
            <DialogPrimitive.Description className="sr-only">{typeof title === "string" ? title : "Dialog"}</DialogPrimitive.Description>
          )}
        </div>
        <DialogPrimitive.Close asChild>
          <Button variant="ghost" size="sm" square icon="x" aria-label="Close" className="-mr-1.5 -mt-0.5" />
        </DialogPrimitive.Close>
      </div>
      <div className="min-h-0 flex-1 overflow-y-auto px-5 py-4">{children}</div>
      {footer && <div className="flex items-center justify-end gap-2 border-t border-border bg-surface-2/40 px-5 py-3">{footer}</div>}
    </>
  );
  return (
    <DialogPrimitive.Root open={open} onOpenChange={onOpenChange}>
      {trigger && <DialogPrimitive.Trigger asChild>{trigger}</DialogPrimitive.Trigger>}
      <DialogPrimitive.Portal>
        <DialogPrimitive.Overlay className={overlay} />
        <DialogPrimitive.Content className={panel} style={{ width: `min(${width}px, calc(100vw - 32px))` }}>
          {onSubmit ? (
            <form
              className="flex min-h-0 flex-1 flex-col"
              onSubmit={(e) => {
                e.preventDefault();
                onSubmit(e);
              }}
            >
              {body}
            </form>
          ) : (
            body
          )}
        </DialogPrimitive.Content>
      </DialogPrimitive.Portal>
    </DialogPrimitive.Root>
  );
}

/** A side sheet for wide editors (connection and agent forms). */
export function Sheet({
  open, onOpenChange, title, description, children, footer, width = 720, onSubmit,
}: {
  open: boolean; onOpenChange: (o: boolean) => void; title: ReactNode; description?: ReactNode;
  children: ReactNode; footer?: ReactNode; width?: number; onSubmit?: (e: FormEvent) => void;
}) {
  const body = (
    <>
      <div className="flex items-start justify-between gap-4 border-b border-border px-6 py-4">
        <div className="min-w-0">
          <DialogPrimitive.Title className="text-[15px] font-semibold text-fg">{title}</DialogPrimitive.Title>
          <DialogPrimitive.Description className={description ? "mt-1 text-sm text-muted" : "sr-only"}>
            {description ?? (typeof title === "string" ? title : "Editor")}
          </DialogPrimitive.Description>
        </div>
        <DialogPrimitive.Close asChild>
          <Button variant="ghost" size="sm" square icon="x" aria-label="Close" />
        </DialogPrimitive.Close>
      </div>
      <div className="min-h-0 flex-1 overflow-y-auto">{children}</div>
      {footer && <div className="flex items-center justify-end gap-2 border-t border-border px-6 py-3">{footer}</div>}
    </>
  );
  return (
    <DialogPrimitive.Root open={open} onOpenChange={onOpenChange}>
      <DialogPrimitive.Portal>
        <DialogPrimitive.Overlay className={overlay} />
        <DialogPrimitive.Content
          className="fixed inset-y-0 right-0 z-50 flex flex-col border-l border-border bg-surface shadow-pop focus:outline-none data-[state=open]:animate-fade-in"
          style={{ width: `min(${width}px, 100vw)` }}
        >
          {onSubmit ? (
            <form className="flex min-h-0 flex-1 flex-col" onSubmit={(e) => { e.preventDefault(); onSubmit(e); }}>
              {body}
            </form>
          ) : body}
        </DialogPrimitive.Content>
      </DialogPrimitive.Portal>
    </DialogPrimitive.Root>
  );
}

export function ConfirmProvider({ children }: { children: ReactNode }) {
  const [state, setState] = useState<(ConfirmOptions & { open: boolean }) | null>(null);
  const [typed, setTyped] = useState("");
  const resolver = useRef<(v: boolean) => void>(() => {});

  const confirm = useCallback((o: ConfirmOptions) => {
    setTyped("");
    setState({ ...o, open: true });
    return new Promise<boolean>((resolve) => {
      resolver.current = resolve;
    });
  }, []);

  const close = (v: boolean) => {
    resolver.current(v);
    setState((s) => (s ? { ...s, open: false } : s));
  };
  const blocked = !!state?.typeToConfirm && typed !== state.typeToConfirm;

  return (
    <ConfirmContext.Provider value={confirm}>
      {children}
      <AlertDialog.Root open={!!state?.open} onOpenChange={(o) => !o && close(false)}>
        <AlertDialog.Portal>
          <AlertDialog.Overlay className={overlay} />
          <AlertDialog.Content className={cn(panel, "w-[min(440px,calc(100vw-32px))]")}>
            <form
              onSubmit={(e) => {
                e.preventDefault();
                if (!blocked) close(true);
              }}
            >
              <div className="flex gap-3 px-5 pb-2 pt-5">
                {state?.tone !== "default" && (
                  <div className="flex size-8 shrink-0 items-center justify-center rounded-full bg-err-soft text-err">
                    <Icon name="alert" size={16} />
                  </div>
                )}
                <div className="min-w-0">
                  <AlertDialog.Title className="text-[15px] font-semibold text-fg">{state?.title}</AlertDialog.Title>
                  <AlertDialog.Description className="mt-1.5 text-sm text-muted">{state?.description ?? "This cannot be undone."}</AlertDialog.Description>
                </div>
              </div>
              {state?.typeToConfirm && (
                <div className="px-5 pt-2">
                  <label className="text-xs text-muted">
                    Type <code className="font-mono text-fg">{state.typeToConfirm}</code> to confirm
                  </label>
                  <Input autoFocus mono value={typed} onChange={(e) => setTyped(e.target.value)} className="mt-1.5" />
                </div>
              )}
              <div className="flex justify-end gap-2 px-5 pb-4 pt-5">
                <AlertDialog.Cancel asChild>
                  <Button variant="secondary">Cancel</Button>
                </AlertDialog.Cancel>
                <Button type="submit" variant={state?.tone === "default" ? "primary" : "danger"} disabled={blocked} autoFocus={!state?.typeToConfirm}>
                  {state?.confirmLabel ?? "Delete"}
                </Button>
              </div>
            </form>
          </AlertDialog.Content>
        </AlertDialog.Portal>
      </AlertDialog.Root>
    </ConfirmContext.Provider>
  );
}

export function Toaster() {
  const items = useToasts();
  return (
    <ToastPrimitive.Provider swipeDirection="right">
      {items.map((t) => (
        <ToastPrimitive.Root
          key={t.id}
          duration={t.duration}
          onOpenChange={(o) => !o && dismissToast(t.id)}
          className="flex w-[360px] items-start gap-3 rounded-lg border border-border bg-surface px-3.5 py-3 shadow-pop data-[state=open]:animate-pop-in"
        >
          <span
            className={cn(
              "mt-0.5 flex size-5 shrink-0 items-center justify-center rounded-full",
              t.tone === "ok" ? "bg-ok-soft text-ok" : t.tone === "err" ? "bg-err-soft text-err" : "bg-info-soft text-info",
            )}
          >
            <Icon name={t.tone === "ok" ? "check" : t.tone === "err" ? "alert" : "info"} size={12} strokeWidth={2.25} />
          </span>
          <div className="min-w-0 flex-1">
            <ToastPrimitive.Title className="text-sm font-medium text-fg">{t.title}</ToastPrimitive.Title>
            {t.description && (
              <ToastPrimitive.Description className="mt-0.5 break-words text-xs text-muted">{t.description}</ToastPrimitive.Description>
            )}
          </div>
          {t.action && (
            <ToastPrimitive.Action altText={t.action.label} asChild>
              <Button size="xs" variant="subtle" onClick={t.action.onClick}>{t.action.label}</Button>
            </ToastPrimitive.Action>
          )}
          <ToastPrimitive.Close aria-label="Dismiss" className="text-subtle hover:text-fg">
            <Icon name="x" size={14} />
          </ToastPrimitive.Close>
        </ToastPrimitive.Root>
      ))}
      <ToastPrimitive.Viewport className="fixed bottom-4 right-4 z-[60] flex flex-col gap-2 outline-none" />
    </ToastPrimitive.Provider>
  );
}

export function Tooltip({ content, children, side = "top" }: { content: ReactNode; children: ReactNode; side?: "top" | "bottom" | "left" | "right" }) {
  if (!content) return <>{children}</>;
  return (
    <TooltipPrimitive.Root delayDuration={300}>
      <TooltipPrimitive.Trigger asChild>{children}</TooltipPrimitive.Trigger>
      <TooltipPrimitive.Portal>
        <TooltipPrimitive.Content
          side={side}
          sideOffset={6}
          className="z-[70] max-w-xs rounded-md bg-fg px-2 py-1 text-xs text-bg shadow-pop data-[state=delayed-open]:animate-fade-in"
        >
          {content}
        </TooltipPrimitive.Content>
      </TooltipPrimitive.Portal>
    </TooltipPrimitive.Root>
  );
}

export function Popover({ trigger, children, align = "start", width, open, onOpenChange }: {
  trigger: ReactNode; children: ReactNode; align?: "start" | "center" | "end"; width?: number;
  open?: boolean; onOpenChange?: (o: boolean) => void;
}) {
  return (
    <PopoverPrimitive.Root open={open} onOpenChange={onOpenChange}>
      <PopoverPrimitive.Trigger asChild>{trigger}</PopoverPrimitive.Trigger>
      <PopoverPrimitive.Portal>
        <PopoverPrimitive.Content
          align={align}
          sideOffset={6}
          style={width ? { width } : undefined}
          className="z-[55] rounded-lg border border-border bg-surface p-1 shadow-pop data-[state=open]:animate-pop-in focus:outline-none"
        >
          {children}
        </PopoverPrimitive.Content>
      </PopoverPrimitive.Portal>
    </PopoverPrimitive.Root>
  );
}

export const Menu = {
  Root: DropdownMenu.Root,
  Trigger: DropdownMenu.Trigger,
  Content: ({ children, align = "end" }: { children: ReactNode; align?: "start" | "end" }) => (
    <DropdownMenu.Portal>
      <DropdownMenu.Content
        align={align}
        sideOffset={6}
        className="z-[55] min-w-[180px] rounded-lg border border-border bg-surface p-1 shadow-pop data-[state=open]:animate-pop-in"
      >
        {children}
      </DropdownMenu.Content>
    </DropdownMenu.Portal>
  ),
  Item: ({ children, onSelect, danger, icon }: { children: ReactNode; onSelect?: () => void; danger?: boolean; icon?: Parameters<typeof Icon>[0]["name"] }) => (
    <DropdownMenu.Item
      onSelect={onSelect}
      className={cn(
        "flex h-8 cursor-default select-none items-center gap-2 rounded-md px-2 text-sm outline-none",
        danger ? "text-err data-[highlighted]:bg-err-soft" : "text-fg data-[highlighted]:bg-surface-2",
      )}
    >
      {icon && <Icon name={icon} size={14} className={danger ? "" : "text-muted"} />}
      {children}
    </DropdownMenu.Item>
  ),
  Separator: () => <DropdownMenu.Separator className="my-1 h-px bg-border" />,
  Label: ({ children }: { children: ReactNode }) => (
    <DropdownMenu.Label className="px-2 pb-1 pt-1.5 text-2xs font-medium uppercase tracking-wide text-subtle">{children}</DropdownMenu.Label>
  ),
};
