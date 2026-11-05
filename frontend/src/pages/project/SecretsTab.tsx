import { SecretsTable } from "@/components/SecretsTable";
import type { Project } from "@/lib/types";

export function SecretsTab({ project }: { project: Project }) {
  return (
    <SecretsTable
      projectId={project.id}
      projectSlug={project.slug}
      title="Project secrets"
      description={
        <>
          Exported into this project's run containers, and resolved first for <code className="font-mono">{"${secret:NAME}"}</code> references. Values are write-only.
        </>
      }
      emptyHint="Tokens for private repositories and credentials your agents' tools need. A project secret wins over a global one with the same name."
    />
  );
}
