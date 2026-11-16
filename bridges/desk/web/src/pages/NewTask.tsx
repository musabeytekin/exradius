import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useNavigate } from "@tanstack/react-router";
import { useState } from "react";
import { FilePicker } from "@/components/FilePicker";
import { Button, Problem, Select } from "@/components/ui";
import { inputClass } from "@/lib/styles";
import { api } from "@/lib/api";
import { qk } from "@/lib/queries";

export function NewTask({ project: initial }: { project?: string }) {
  const navigate = useNavigate();
  const qc = useQueryClient();
  const me = useQuery({ queryKey: qk.me, queryFn: api.me, staleTime: 60_000 });
  const projects = useQuery({ queryKey: qk.projects, queryFn: api.projects });
  const [project, setProject] = useState(initial ?? "");
  const chosen = project || (projects.data?.length === 1 ? projects.data[0].slug : "");
  const people = useQuery({ queryKey: qk.people(chosen), queryFn: () => api.people(chosen), enabled: !!chosen });
  const [title, setTitle] = useState("");
  const [description, setDescription] = useState("");
  const [assignee, setAssignee] = useState("none");
  const [files, setFiles] = useState<File[]>([]);

  const create = useMutation({
    mutationFn: () => api.create({ project: chosen, title, description, assignee }, files),
    onSuccess: ({ key }) => {
      qc.invalidateQueries({ queryKey: ["tasks"] });
      navigate({ to: "/tasks/$key", params: { key } });
    },
  });
  const selected = projects.data?.find((p) => p.slug === chosen);

  return (
    <form
      className="mx-auto max-w-2xl space-y-4"
      onSubmit={(e) => {
        e.preventDefault();
        create.mutate();
      }}
    >
      <h1 className="text-lg font-semibold tracking-[-0.01em]">New task</h1>
      <div className="grid gap-3 sm:grid-cols-2">
        <label className="space-y-1">
          <span className="text-xs font-medium text-muted">Project</span>
          <Select className="w-full" value={chosen} required onChange={(e) => {
            setProject(e.target.value);
            setAssignee((a) => (a.startsWith("person:") ? "none" : a));
          }}>
            <option value="" disabled>Pick a project</option>
            {projects.data?.map((p) => <option key={p.slug} value={p.slug}>{p.name}{p.runnable ? "" : " (not runnable)"}</option>)}
          </Select>
        </label>
        <label className="space-y-1">
          <span className="text-xs font-medium text-muted">Assignee</span>
          <Select className="w-full" value={assignee} onChange={(e) => setAssignee(e.target.value)}>
            <option value="none">Unassigned</option>
            <option value="exradius">{me.data?.exradius ?? "Exradius"}</option>
            {people.data?.map((p) => <option key={p.id} value={`person:${p.id}`}>{p.id === me.data?.id ? `${p.name} (me)` : p.name}</option>)}
          </Select>
        </label>
      </div>
      {selected && !selected.runnable && assignee === "exradius" && (
        <p className="text-sm text-warn">{selected.name} is not runnable now.</p>
      )}
      <label className="block space-y-1">
        <span className="text-xs font-medium text-muted">Title</span>
        <input className={inputClass} value={title} required maxLength={300} onChange={(e) => setTitle(e.target.value)} />
      </label>
      <label className="block space-y-1">
        <span className="text-xs font-medium text-muted">Description</span>
        <textarea className={`${inputClass} min-h-40 resize-y`} value={description} onChange={(e) => setDescription(e.target.value)} />
      </label>
      <FilePicker files={files} onChange={setFiles} />
      {create.error && <Problem>{create.error.message}</Problem>}
      <div className="flex justify-end gap-2">
        <Button onClick={() => navigate({ to: "/tasks" })}>Discard</Button>
        <Button type="submit" variant="primary" disabled={!chosen || !title.trim() || create.isPending}>Create</Button>
      </div>
    </form>
  );
}
