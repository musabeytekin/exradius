import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link } from "@tanstack/react-router";
import { useMemo, useRef, useState } from "react";
import { FilePicker } from "@/components/FilePicker";
import { Markdown } from "@/components/Markdown";
import { MentionBox } from "@/components/MentionBox";
import { AssigneeLabel, RunChip } from "@/components/TaskBits";
import { Avatar, Button, Chip, Loading, Problem, Select } from "@/components/ui";
import { inputClass } from "@/lib/styles";
import { api, artifactUrl, attachmentUrl } from "@/lib/api";
import { STATUSES, STATUS_LABEL, ago, exact, size } from "@/lib/format";
import { encode, type Candidate } from "@/lib/mentions";
import { qk } from "@/lib/queries";
import type { ChangeEntry, CommentEntry, FileRef, Status, Task } from "@/lib/types";

const EXRADIUS_TOKEN = "@[exradius]";

export function TaskPage({ taskKey }: { taskKey: string }) {
  const task = useQuery({ queryKey: qk.task(taskKey), queryFn: () => api.task(taskKey) });
  if (task.isPending) return <Loading />;
  if (task.error) return <Problem>{task.error.message}</Problem>;
  return <TaskView task={task.data} />;
}

function useTaskMutation<T>(key: string, fn: (value: T) => Promise<unknown>) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: fn,
    onSettled: () => {
      qc.invalidateQueries({ queryKey: qk.task(key) });
      qc.invalidateQueries({ queryKey: ["tasks"] });
    },
  });
}

function TaskView({ task }: { task: Task }) {
  const me = useQuery({ queryKey: qk.me, queryFn: api.me, staleTime: 60_000 });
  const people = useQuery({ queryKey: qk.people(task.project), queryFn: () => api.people(task.project) });
  const exradiusName = me.data?.exradius ?? "Exradius";
  const live = task.run;

  const [editing, setEditing] = useState(false);
  const [title, setTitle] = useState(task.title);
  const [description, setDescription] = useState(task.description);
  const edit = useTaskMutation(task.key, (changes: { title?: string; description?: string; status?: string }) =>
    api.edit(task.key, changes));
  const assign = useTaskMutation(task.key, (assignee: string) => api.assign(task.key, assignee));
  const cancel = useTaskMutation(task.key, () => api.cancel(task.key));
  const addFiles = useTaskMutation(task.key, (files: File[]) => api.addFiles(task.key, files));

  const [text, setText] = useState("");
  const [chosen, setChosen] = useState<Candidate[]>([]);
  const [files, setFiles] = useState<File[]>([]);
  const box = useRef<HTMLTextAreaElement>(null);
  const comment = useTaskMutation(task.key, () => api.comment(task.key, encode(text, chosen), files));

  const candidates = useMemo<Candidate[]>(() => [
    ...(task.exradius ? [{ token: EXRADIUS_TOKEN, name: exradiusName, exradius: true }] : []),
    ...(people.data ?? []).map((p) => ({ token: `@[person:${p.id}]`, name: p.name })),
  ], [task.exradius, exradiusName, people.data]);

  function answer() {
    const candidate = { token: EXRADIUS_TOKEN, name: exradiusName, exradius: true };
    setChosen((c) => [...c, candidate]);
    setText((t) => (t.includes(`@${exradiusName}`) ? t : `@${exradiusName} ${t}`));
    box.current?.focus();
    requestAnimationFrame(() => {
      const end = box.current?.value.length ?? 0;
      box.current?.setSelectionRange(end, end);
    });
  }

  const assigneeValue = task.assignee.kind === "person" ? `person:${task.assignee.id}` : task.assignee.kind;
  const mentionsExradius = encode(text, chosen).includes(EXRADIUS_TOKEN);

  return (
    <div className="space-y-4">
        <div className="space-y-2">
          <div className="flex items-center gap-2 text-sm text-subtle">
            <Link to="/tasks" search={{ project: task.project }} className="hover:text-fg">{task.project_name}</Link>
            <span>/</span>
            <span className="font-mono text-xs">{task.key}</span>
          </div>
          {editing ? (
            <form className="space-y-2" onSubmit={(e) => {
              e.preventDefault();
              edit.mutate({ title, description }, { onSuccess: () => setEditing(false) });
            }}>
              <input aria-label="Title" className={`${inputClass} text-lg font-semibold`} value={title} maxLength={300}
                onChange={(e) => setTitle(e.target.value)} />
              <textarea aria-label="Description" className={`${inputClass} min-h-40 resize-y`} value={description}
                onChange={(e) => setDescription(e.target.value)} />
              {edit.error && <Problem>{edit.error.message}</Problem>}
              <div className="flex justify-end gap-2">
                <Button onClick={() => { setEditing(false); setTitle(task.title); setDescription(task.description); }}>Discard</Button>
                <Button type="submit" variant="primary" disabled={!title.trim() || edit.isPending}>Save</Button>
              </div>
            </form>
          ) : (
            <>
              <div className="flex items-start gap-2">
                <h1 className="flex-1 text-xl font-semibold tracking-[-0.01em]">{task.title}</h1>
                <Button small variant="ghost" onClick={() => { setTitle(task.title); setDescription(task.description); setEditing(true); }}>Edit</Button>
              </div>
            </>
          )}
        </div>
    <div className="grid gap-6 lg:grid-cols-[minmax(0,1fr)_260px]">
      <div className="min-w-0 space-y-5">
        <div className="space-y-2">
          {!editing && task.description && <Markdown text={task.description} />}
          <FileList files={task.files} />
          <FilePicker files={[]} label="Add files" onChange={(f) => f.length && addFiles.mutate(f)} />
          {addFiles.error && <Problem>{addFiles.error.message}</Problem>}
        </div>

        {live && (
          <section aria-label="Run" className="flex flex-wrap items-center gap-2 rounded-lg border border-border bg-surface px-3 py-2.5">
            <Avatar name={exradiusName} exradius />
            <span className="font-medium">{live.agent_name ?? exradiusName}</span>
            <RunChip state={live.state} />
            <span className="text-xs text-subtle">run {live.seq}</span>
            <span className="ml-auto" />
            {live.cancel_requested_by
              ? <span className="text-sm text-muted">{live.cancel_requested_by.name} asked it to stop</span>
              : <Button small variant="danger" disabled={cancel.isPending} onClick={() => cancel.mutate(undefined)}>Cancel run</Button>}
            {cancel.error && <Problem>{cancel.error.message}</Problem>}
          </section>
        )}

        <section aria-label="Activity" className="space-y-3">
          {task.timeline.map((entry) => entry.type === "change"
            ? <Change key={`h${entry.id}`} entry={entry} task={task} exradiusName={exradiusName} />
            : <CommentCard key={`c${entry.id}`} entry={entry} task={task} exradiusName={exradiusName} onAnswer={answer} />)}
        </section>

        <form className="space-y-2 rounded-lg border border-border bg-surface p-3" onSubmit={(e) => {
          e.preventDefault();
          comment.mutate(undefined, { onSuccess: () => { setText(""); setChosen([]); setFiles([]); } });
        }}>
          <MentionBox label="Comment" textarea={box} value={text} onChange={setText} candidates={candidates}
            onChoose={(c) => setChosen((list) => [...list, c])} placeholder="Comment" />
          <div className="flex flex-wrap items-center gap-2">
            <FilePicker files={files} onChange={setFiles} />
            <span className="ml-auto" />
            {mentionsExradius && task.exradius === "start" && <span className="text-xs text-muted">Starts run {task.runs.length + 1}</span>}
            {mentionsExradius && task.exradius === "answer" && <span className="text-xs text-muted">Answers the question</span>}
            <Button type="submit" variant="primary" small disabled={(!text.trim() && !files.length) || comment.isPending}>Comment</Button>
          </div>
          {comment.error && <Problem>{comment.error.message}</Problem>}
        </form>
      </div>

      <aside className="order-first grid grid-cols-2 gap-3 rounded-lg border border-border bg-surface p-3 lg:order-none lg:sticky lg:top-20 lg:block lg:space-y-4 lg:self-start lg:border-0 lg:bg-transparent lg:p-0">
        <Field label="Status">
          <Select aria-label="Status" className="w-full" value={task.status} disabled={edit.isPending}
            onChange={(e) => edit.mutate({ status: e.target.value as Status })}>
            {STATUSES.map((s) => <option key={s} value={s}>{STATUS_LABEL[s]}</option>)}
          </Select>
        </Field>
        <Field label="Assignee">
          <Select aria-label="Assignee" className="w-full" value={assigneeValue} disabled={!!live || assign.isPending}
            onChange={(e) => assign.mutate(e.target.value)}>
            <option value="none">Unassigned</option>
            <option value="exradius">{exradiusName}</option>
            {people.data?.map((p) => <option key={p.id} value={`person:${p.id}`}>{p.name}</option>)}
            {task.assignee.kind === "person" && !people.data?.some((p) => p.id === task.assignee.id) &&
              <option value={assigneeValue}>{task.assignee.name}</option>}
          </Select>
          {assign.error && <Problem>{assign.error.message}</Problem>}
        </Field>
        <Field label="Reporter">{task.reporter ? <AssigneeLabel assignee={{ kind: "person", ...task.reporter }} /> : null}</Field>
        <Field label="Created"><span title={exact(task.created_at)}>{ago(task.created_at)}</span></Field>
        {task.runs.length > 0 && (
          <Field label="Runs" wide>
            <ul className="space-y-1">
              {task.runs.map((r) => (
                <li key={r.seq} className="flex items-center gap-2 text-sm">
                  <span className="text-subtle">{r.seq}</span>
                  <span className="truncate">{r.agent_name ?? exradiusName}</span>
                  <RunChip state={r.state} />
                </li>
              ))}
            </ul>
          </Field>
        )}
      </aside>
    </div>
    </div>
  );
}

function Field({ label, wide, children }: { label: string; wide?: boolean; children: React.ReactNode }) {
  return (
    <div className={wide ? "col-span-2 space-y-1" : "space-y-1"}>
      <div className="text-xs font-medium text-muted">{label}</div>
      <div className="text-sm">{children}</div>
    </div>
  );
}

function FileList({ files }: { files: FileRef[] }) {
  if (!files.length) return null;
  return (
    <ul className="flex flex-wrap gap-2">
      {files.map((f) => (
        <li key={f.id}>
          <a href={attachmentUrl(f.id)} className="inline-flex items-center gap-1.5 rounded-md border border-border bg-surface px-2 py-1 text-sm hover:border-border-strong">
            {f.name} <span className="text-xs text-subtle">{size(f.size)}</span>
          </a>
        </li>
      ))}
    </ul>
  );
}

function assigneeName(value: string | null, task: Task, exradiusName: string): string {
  if (!value || value === "none") return "nobody";
  if (value === "exradius") return exradiusName;
  return task.names[value.slice(7)] ?? "someone";
}

function Change({ entry, task, exradiusName }: { entry: ChangeEntry; task: Task; exradiusName: string }) {
  const who = entry.person?.name ?? "Desk";
  let what: string;
  if (entry.field === "assignee") {
    what = `assigned it to ${assigneeName(entry.new, task, exradiusName)}`;
  } else if (entry.field === "status") {
    what = `moved it from ${STATUS_LABEL[entry.old as Status] ?? entry.old} to ${STATUS_LABEL[entry.new as Status] ?? entry.new}`;
  } else {
    what = `changed the ${entry.field}`;
  }
  return (
    <p className="pl-8 text-xs text-subtle">
      <span className="text-muted">{who}</span> {what} · <span title={exact(entry.created_at)}>{ago(entry.created_at)}</span>
    </p>
  );
}

const RESULT_HEADING: Record<string, string> = { completed: "finished", failed: "could not finish", cancelled: "was stopped" };

function CommentCard({ entry, task, exradiusName, onAnswer }:
  { entry: CommentEntry; task: Task; exradiusName: string; onAnswer: () => void }) {
  const agent = entry.agent_name ?? exradiusName;
  const exradius = entry.author.kind === "exradius";
  const answeredBy = entry.answered_by ? task.timeline.find((e) => e.type === "comment" && e.id === entry.answered_by) : null;
  const questionOpen = entry.kind === "question" && !entry.answered_by && task.exradius === "answer";

  let heading: React.ReactNode = <span className="font-medium">{entry.author.name}</span>;
  let body: React.ReactNode = entry.body ? <Markdown text={entry.body} names={task.names} /> : null;
  let tone = "border-border bg-surface";

  if (entry.kind === "started") {
    const guess = entry.data.guess as string | null;
    return (
      <p className="flex flex-wrap items-center gap-1.5 pl-8 text-sm text-muted">
        <span className="font-medium text-fg">{agent}</span> is working on this
        {entry.run && <span className="text-xs text-subtle">run {entry.run}</span>}
        {guess && <Chip tone="warn">{guess}</Chip>}
        <span className="text-xs text-subtle">· {ago(entry.created_at)}</span>
      </p>
    );
  }
  if (entry.kind === "cancel") {
    return <p className="pl-8 text-xs text-subtle"><span className="text-muted">{entry.author.name}</span> asked {exradiusName} to stop · {ago(entry.created_at)}</p>;
  }
  if (entry.kind === "question") {
    heading = <><span className="font-medium">{agent}</span> <span className="text-muted">asks</span></>;
    const context = entry.data.context as string | undefined;
    body = (
      <>
        <Markdown text={entry.body} />
        {context && <div className="text-muted"><Markdown text={context} /></div>}
      </>
    );
    tone = questionOpen ? "border-warn bg-warn-soft/40" : tone;
  } else if (entry.kind === "result") {
    const state = entry.data.state as string;
    const error = entry.data.error as string | null;
    const warnings = (entry.data.warnings as string[] | undefined) ?? [];
    heading = <><span className="font-medium">{agent}</span> <span className="text-muted">{RESULT_HEADING[state] ?? "ended"}</span></>;
    tone = state === "completed" ? "border-ok/40 bg-surface" : state === "failed" ? "border-err/40 bg-surface" : tone;
    body = (
      <>
        {error && <pre className="overflow-x-auto whitespace-pre-wrap rounded-md bg-err-soft p-2 font-mono text-xs text-err">{error}</pre>}
        {entry.body && <Markdown text={entry.body} />}
        {warnings.length > 0 && <ul className="list-disc pl-5 text-sm text-warn">{warnings.map((w) => <li key={w}>{w}</li>)}</ul>}
        {entry.artifacts.length > 0 && (
          <ul className="flex flex-wrap gap-2">
            {entry.artifacts.map((a) => (
              <li key={a.id}>
                <a href={artifactUrl(a.id)} className="inline-flex items-center rounded-md border border-border bg-surface px-2 py-1 text-sm hover:border-border-strong">{a.name}</a>
              </li>
            ))}
          </ul>
        )}
      </>
    );
  } else if (entry.kind === "rejected") {
    heading = <span className="font-medium text-err">Could not start</span>;
    body = <p className="text-sm">{entry.body}</p>;
    tone = "border-err/40 bg-surface";
  }

  return (
    <article id={`comment-${entry.id}`} className="flex gap-2">
      <Avatar name={entry.author.name} exradius={exradius} />
      <div className={`min-w-0 flex-1 space-y-2 rounded-lg border px-3 py-2 ${tone}`}>
        <header className="flex flex-wrap items-center gap-x-2 text-sm">
          {heading}
          <span className="text-xs text-subtle" title={exact(entry.created_at)}>{ago(entry.created_at)}</span>
          {entry.answers && <Chip tone="accent">answered the question</Chip>}
          {entry.started_run && <Chip tone="accent">started run {entry.started_run}</Chip>}
          {entry.kind === "question" && answeredBy && answeredBy.type === "comment" &&
            <Chip tone="ok">answered by {answeredBy.author.name}</Chip>}
        </header>
        {body}
        <FileList files={entry.files} />
        {questionOpen && <Button small variant="primary" onClick={onAnswer}>Answer</Button>}
      </div>
    </article>
  );
}
