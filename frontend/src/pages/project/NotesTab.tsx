import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { Button, Card, CardHeader, EmptyState, Textarea } from "@/components/ui/primitives";
import { api } from "@/lib/api";
import { qk } from "@/lib/queries";
import { toast } from "@/lib/toast";
import type { Project } from "@/lib/types";

/** `projects.notes`, quoted at the end of the agent's environment section. */
export function NotesTab({ project }: { project: Project }) {
  const qc = useQueryClient();
  const supported = typeof project.notes === "string";
  const stored = project.notes ?? "";
  const [notes, setNotes] = useState(stored);
  const dirty = supported && notes !== stored;
  const save = useMutation({
    mutationFn: () => api.projects.update(project.id, { notes }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: qk.projects });
      toast.ok("Notes saved", "Runs started from now on are told them.");
    },
    onError: (e) => toast.err("Could not save the notes", e),
  });
  return (
    <Card>
      <CardHeader
        title="Notes for the agent"
        description="Quoted at the end of what every run of this project is told about its environment."
        actions={
          supported && (
            <>
              {dirty && <Button size="sm" variant="ghost" onClick={() => setNotes(stored)}>Discard</Button>}
              <Button size="sm" variant="primary" disabled={!dirty} loading={save.isPending} onClick={() => save.mutate()}>Save</Button>
            </>
          )
        }
      />
      {supported ? (
        <div className="flex flex-col gap-1.5 p-4">
          <Textarea
            aria-label="Notes"
            rows={14}
            value={notes}
            onChange={(e) => setNotes(e.target.value)}
            onKeyDown={(e) => {
              if ((e.metaKey || e.ctrlKey) && e.key === "s") {
                e.preventDefault();
                if (dirty) save.mutate();
              }
            }}
            placeholder={"Run the API tests with ./mvnw -pl ledger test.\nThe frontend talks to the API through /api; never call services directly."}
            className="resize-y"
          />
          <p className="text-xs text-subtle">Write what you would tell a new engineer on their first day.</p>
        </div>
      ) : (
        <EmptyState compact icon="note" title="Not available on this API">
          This API does not store project notes yet. Update Exradius to tell agents about the project in your own words.
        </EmptyState>
      )}
    </Card>
  );
}
