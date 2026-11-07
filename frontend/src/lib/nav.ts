import type { IconName } from "@/components/ui/icons";

export const NAV: { to: string; label: string; icon: IconName; hint: string }[] = [
  { to: "/", label: "Overview", icon: "overview", hint: "System status and project health" },
  { to: "/projects", label: "Projects", icon: "projects", hint: "Config repositories" },
  { to: "/stores", label: "Stores", icon: "key", hint: "Secret stores" },
  { to: "/providers", label: "Providers", icon: "brain", hint: "Model credentials" },
  { to: "/tasks", label: "Tasks", icon: "tasks", hint: "Runs and transcripts" },
  { to: "/settings", label: "Settings", icon: "settings", hint: "Password, config repository access" },
];
