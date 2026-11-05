import type { IconName } from "@/components/ui/icons";

export const NAV: { to: string; label: string; icon: IconName; hint: string }[] = [
  { to: "/", label: "Overview", icon: "overview", hint: "System status and setup" },
  { to: "/projects", label: "Projects", icon: "projects", hint: "Repositories, environment, secrets" },
  { to: "/connections", label: "Connections", icon: "connections", hint: "MCP servers" },
  { to: "/providers", label: "Providers", icon: "brain", hint: "Model credentials" },
  { to: "/agents", label: "Agents", icon: "agents", hint: "Prompts and connections" },
  { to: "/tasks", label: "Tasks", icon: "tasks", hint: "Runs and transcripts" },
  { to: "/secrets", label: "Secrets", icon: "key", hint: "Global secrets" },
  { to: "/integration", label: "Integration", icon: "integration", hint: "Broker, schemas, live events" },
  { to: "/settings", label: "Settings", icon: "settings", hint: "Infrastructure values" },
];
