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
        description={<>Referenced as <code className="font-mono">{"${secret:NAME}"}</code> in connection env and header values. A project secret with the same name wins.</>}
        emptyHint={<>Tokens that connections in every project reference, such as <code className="font-mono text-xs">GITHUB_TOKEN</code>.</>}
      />
    </>
  );
}
