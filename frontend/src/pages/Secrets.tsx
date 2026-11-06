import { useSearch } from "@tanstack/react-router";
import { SecretsTable } from "@/components/SecretsTable";
import { PageHeader } from "@/components/ui/primitives";

export function SecretsPage() {
  const { create } = useSearch({ from: "/app/secrets" });
  return (
    <>
      <PageHeader
        title="Global secrets"
        description="Shared credentials, used only by reference from connections. They are never exported into a run's environment."
      />
      <SecretsTable
        projectId={null}
        title="Secrets"
        openCreate={!!create}
        createKey={create && create !== "1" ? create : undefined}
        description={<>Catalog connections and repositories create these for you. Custom connections reference them by name as <code className="font-mono">{"${secret:NAME}"}</code>; a project secret with the same name wins.</>}
        emptyHint="Tokens that catalog connections and repositories use. Adding a GitHub or Jira connection creates them for you."
      />
    </>
  );
}
