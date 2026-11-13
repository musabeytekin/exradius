// The shapes of Desk's API (exradius_desk/app.py, desk.py).

export type Status = "todo" | "in_progress" | "done";
export type RunState =
  | "sent" | "queued" | "preparing" | "running" | "waiting_for_input" | "resuming"
  | "completed" | "failed" | "cancelled" | "rejected";

export interface Me {
  id: number;
  name: string;
  email: string | null;
  groups: string[];
  exradius: string;
}

export interface Project {
  slug: string;
  name: string;
  runnable: boolean;
}

export interface PersonRef {
  id: number;
  name: string;
}

export interface Assignee {
  kind: "none" | "person" | "exradius";
  id: number | null;
  name: string | null;
}

export interface TaskItem {
  key: string;
  project: string;
  title: string;
  status: Status;
  assignee: Assignee;
  updated_at: string;
  run_state: RunState | null;
}

export interface FileRef {
  id: number;
  name: string;
  content_type: string;
  size: number;
  uploaded_by: PersonRef | null;
  created_at: string;
}

export interface Run {
  seq: number;
  task_id: string;
  state: RunState;
  agent: string | null;
  agent_name: string | null;
  routing: Record<string, unknown> | null;
  created_at: string;
  ended_at: string | null;
  handed_over_by: PersonRef;
  cancel_requested_by: PersonRef | null;
}

export type CommentKind = "comment" | "question" | "started" | "result" | "rejected" | "cancel";

export interface CommentEntry {
  type: "comment";
  id: number;
  kind: CommentKind;
  body: string;
  data: Record<string, unknown>;
  author: { kind: "person" | "exradius"; id: number | null; name: string };
  created_at: string;
  run: number | null;
  agent_name: string | null;
  files: FileRef[];
  artifacts: { id: number; name: string; content_type: string }[];
  request_id?: string;
  answered_by?: number | null;
  answers?: number | null;
  started_run?: number | null;
}

export interface ChangeEntry {
  type: "change";
  id: number;
  field: "title" | "description" | "status" | "assignee";
  old: string | null;
  new: string | null;
  person: PersonRef | null;
  created_at: string;
}

export type Entry = CommentEntry | ChangeEntry;

export interface Task {
  key: string;
  project: string;
  project_name: string;
  title: string;
  description: string;
  status: Status;
  reporter: PersonRef | null;
  assignee: Assignee;
  handed_over_by: PersonRef | null;
  created_at: string;
  updated_at: string;
  files: FileRef[];
  run: Run | null;
  runs: Run[];
  timeline: Entry[];
  names: Record<string, string>;
  /** What a mention of Exradius would do now: answer the open question, start a run, or nothing. */
  exradius: "answer" | "start" | null;
}
