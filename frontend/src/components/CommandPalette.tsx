import * as DialogPrimitive from "@radix-ui/react-dialog";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useNavigate } from "@tanstack/react-router";
import { Command } from "cmdk";
import { useState, type ReactNode } from "react";
import { api } from "@/lib/api";
import { projectName } from "@/lib/projectConfig";
import { qk, useProjects, useStores } from "@/lib/queries";
import { setTheme, useTheme } from "@/lib/theme";
import { NAV } from "@/lib/nav";
import { Icon, type IconName } from "./ui/icons";
import { Kbd } from "./ui/primitives";
import { StateChip } from "./ui/data";


export function CommandPalette({ open, onOpenChange }: { open: boolean; onOpenChange: (o: boolean) => void }) {
  return (
    <DialogPrimitive.Root open={open} onOpenChange={onOpenChange}>
      <DialogPrimitive.Portal>
        <DialogPrimitive.Overlay className="fixed inset-0 z-40 bg-[rgb(10_11_14/0.4)] backdrop-blur-[2px] data-[state=open]:animate-fade-in" />
        <DialogPrimitive.Content className="fixed left-1/2 top-[14vh] z-50 w-[min(620px,calc(100vw-32px))] -translate-x-1/2 overflow-hidden rounded-xl border border-border bg-surface shadow-pop focus:outline-none data-[state=open]:animate-pop-in">
          <DialogPrimitive.Title className="sr-only">Command palette</DialogPrimitive.Title>
          <DialogPrimitive.Description className="sr-only">Jump to a page or entity, or run an action.</DialogPrimitive.Description>
          {/* Content unmounts when closed, so the search resets every time. */}
          <PaletteBody close={() => onOpenChange(false)} />
        </DialogPrimitive.Content>
      </DialogPrimitive.Portal>
    </DialogPrimitive.Root>
  );
}

function PaletteBody({ close }: { close: () => void }) {
  const navigate = useNavigate();
  const qc = useQueryClient();
  const { pref } = useTheme();
  const [search, setSearch] = useState("");
  const projects = useProjects();
  const stores = useStores();
  const tasks = useQuery({ queryKey: qk.tasks({ limit: 8 }), queryFn: () => api.tasks.list({ limit: 8 }) });

  const go = (fn: () => void) => {
    close();
    fn();
  };

  return (
          <Command label="Command palette" loop className="flex flex-col">
            <div className="flex items-center gap-2.5 border-b border-border px-4">
              <Icon name="search" size={16} className="text-subtle" />
              <Command.Input
                value={search}
                onValueChange={setSearch}
                autoFocus
                placeholder="Jump to a page, project, store or task…"
                className="h-12 flex-1 bg-transparent text-[14px] text-fg placeholder:text-subtle focus:outline-none"
              />
              <Kbd>esc</Kbd>
            </div>
            <Command.List className="max-h-[420px] overflow-y-auto p-1.5 [&_[cmdk-group-heading]]:px-2.5 [&_[cmdk-group-heading]]:pb-1 [&_[cmdk-group-heading]]:pt-2.5 [&_[cmdk-group-heading]]:text-2xs [&_[cmdk-group-heading]]:font-medium [&_[cmdk-group-heading]]:uppercase [&_[cmdk-group-heading]]:tracking-wide [&_[cmdk-group-heading]]:text-subtle">
              <Command.Empty className="px-3 py-8 text-center text-sm text-muted">Nothing matches “{search}”.</Command.Empty>
              <Command.Group heading="Actions">
                <Item icon="send" onSelect={() => go(() => navigate({ to: "/tasks", search: { send: "1" } }))} keywords={["task", "run", "publish"]}>Send a task</Item>
                <Item icon="plus" onSelect={() => go(() => navigate({ to: "/projects", search: { create: "1" } }))} keywords={["new", "config", "repository"]}>Add a project</Item>
                <Item icon="key" onSelect={() => go(() => navigate({ to: "/stores", search: { create: "1" } }))} keywords={["new", "secret"]}>Create a store</Item>
                <Item icon="brain" onSelect={() => go(() => navigate({ to: "/providers", search: { create: "1" } }))} keywords={["new", "model", "anthropic", "credential"]}>Add a provider</Item>
                <Item icon={pref === "dark" ? "sun" : "moon"} onSelect={() => go(() => setTheme(pref === "dark" ? "light" : "dark"))} keywords={["theme", "dark", "light"]}>
                  Switch to {pref === "dark" ? "light" : "dark"} theme
                </Item>
                <Item icon="monitor" onSelect={() => go(() => setTheme("system"))} keywords={["theme"]}>Follow the system theme</Item>
                <Item
                  icon="logout"
                  onSelect={() => go(async () => {
                    await api.auth.logout().catch(() => {});
                    qc.clear();
                    navigate({ to: "/login", search: { next: undefined } });
                  })}
                >
                  Log out
                </Item>
              </Command.Group>
              <Command.Group heading="Pages">
                {NAV.map((n) => (
                  <Item key={n.to} icon={n.icon} hint={n.hint} onSelect={() => go(() => navigate({ to: n.to }))}>{n.label}</Item>
                ))}
              </Command.Group>
              {!!projects.data?.length && (
                <Command.Group heading="Projects">
                  {projects.data.map((p) => (
                    <Item key={p.id} icon="projects" value={`project ${p.slug} ${projectName(p)}`} hint={p.slug} onSelect={() => go(() => navigate({ to: "/projects/$projectId", params: { projectId: p.id }, search: {} }))}>
                      {projectName(p)}
                    </Item>
                  ))}
                </Command.Group>
              )}
              {!!stores.data?.length && (
                <Command.Group heading="Stores">
                  {stores.data.map((st) => (
                    <Item key={st.id} icon="key" value={`store ${st.name}`} onSelect={() => go(() => navigate({ to: "/stores/$storeId", params: { storeId: st.id } }))}>
                      <span className="font-mono text-[12.5px]">{st.name}</span>
                    </Item>
                  ))}
                </Command.Group>
              )}
              {!!tasks.data?.length && (
                <Command.Group heading="Recent tasks">
                  {tasks.data.map((t) => (
                    <Item
                      key={t.task_id}
                      icon="tasks"
                      value={`task ${t.task_id} ${t.project} ${t.agent}`}
                      hint={<StateChip state={t.state} />}
                      onSelect={() => go(() => navigate({ to: "/tasks/$taskId", params: { taskId: t.task_id } }))}
                    >
                      <span className="font-mono text-[12.5px]">{t.task_id}</span>
                    </Item>
                  ))}
                </Command.Group>
              )}
            </Command.List>
            <div className="flex items-center gap-3 border-t border-border px-4 py-2 text-2xs text-subtle">
              <span className="flex items-center gap-1"><Kbd>↑</Kbd><Kbd>↓</Kbd> navigate</span>
              <span className="flex items-center gap-1"><Kbd>↵</Kbd> open</span>
            </div>
          </Command>
  );
}

function Item({ children, icon, onSelect, hint, value, keywords }: {
  children: ReactNode; icon: IconName; onSelect: () => void; hint?: ReactNode; value?: string; keywords?: string[];
}) {
  return (
    <Command.Item
      value={value}
      keywords={keywords}
      onSelect={onSelect}
      className="flex h-9 cursor-default select-none items-center gap-2.5 rounded-md px-2.5 text-sm text-fg data-[selected=true]:bg-surface-2"
    >
      <Icon name={icon} size={15} className="shrink-0 text-muted" />
      <span className="min-w-0 flex-1 truncate">{children}</span>
      {hint && <span className="shrink-0 text-xs text-subtle">{hint}</span>}
    </Command.Item>
  );
}
