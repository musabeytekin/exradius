# Exradius Desk design

Status: agreed 2026-11-14. Revised the same day: Desk is a small task system with
assignment, comments and @mentions, like Jira, not a request inbox. Internal design note.
Exradius Desk (`exradius-desk`) is Exradius's own task bridge. It builds on
`exradius-bridge-kit` (its `docs/design.md`, cited as "kit §n"). The Exradius side is
`docs/architecture.md` §4 (messages), `docs/integration-api.md` (directory, bridge status,
cancel) and `docs/agent-routing.md` in the Exradius repository.

## 1. What it is

The other bridges connect an outside tracker (Jira, GitHub) to Exradius. Desk is the
tracker as well as the bridge, so a team without Jira or GitHub can still keep its work
somewhere and hand some of it to agents.

- A **task** exists on its own, whether or not an agent ever works on it. It has a
  project, a key (`plane-12`), a title, a description, attachments, a reporter, an
  assignee, a status and comments.
- The **assignee** is nobody, a person, or **Exradius**. Exradius is one built-in
  assignee, not one per agent: routing picks the agent (agent-routing.md), and the task
  shows which one it picked.
- **Assigning a task to Exradius starts a run.** So does a comment that @mentions Exradius
  while no run is live (§7). When the run ends, Exradius posts the result as a comment and
  assigns the task back to the person who handed it over, as the Jira bridge does
  (kit §6).
- The agent's **questions** are comments by Exradius, marked as questions. They are
  answered by a comment that mentions Exradius. The Answer action on a question opens the
  comment box with that mention already in it.
- The **status** is To do, In progress or Done, and people move it.

It is not a full tracker: §16 lists what it leaves out. Like the panel, Desk shows state,
not help: it has no guides, tips or empty-state explanations.

Desk is three things in one image:

- a FastAPI service with its own database, on the kit's broker and integration API
  clients;
- a small React frontend (`bridges/desk/web`), built into the image and served by the same
  process;
- the chart component that runs it (§13).

Like every bridge, it speaks only the public contracts and imports nothing from
`backend/`:

- the broker: `exradius.tasks`, `exradius.answers` and `exradius.cancels` out, and
  `exradius.events` in;
- the integration API: the directory with `bridges/desk.yaml`, the `directory.changed`
  notice, and the bridge status report;
- the signed download URLs of `task.result`.

## 2. Who sees which project: `bridges/desk.yaml`

A project appears in Desk only when its config repository has `bridges/desk.yaml`
(config-repo.md §2.1). The file names the identity provider's groups whose members may use
the project:

```yaml
groups: [payments-team, platform-leads]
```

- `groups`: group names exactly as the identity provider puts them in the groups claim
  (§3), at least one, each 1 to 200 characters, no repeats. This is the only key in v1, and
  unknown keys are errors.
- Validation is the kit's: strict pydantic, and every problem is reported through the
  bridge status with its path (`bridges/desk.yaml: groups.1: listed more than once`).
  A file Exradius could not read (`bridge_config_error`) is reported as given.
- A project whose file is broken or invalid appears to nobody until it is fixed. Its tasks
  stay in Desk's database and come back with the file.
- **Visibility is per project.** Someone in at least one of a project's groups sees all of
  its tasks and may do everything with them: create, edit, comment, assign, move,
  answer and cancel. Nobody else sees that the project exists.
- Access is decided on every request, from the latest directory and the groups recorded at
  the person's last sign-in (§3). Removing a group from the file takes effect with the next
  directory fetch. Removing a person from a group at the identity provider takes effect at
  their next sign-in.
- A project that is not runnable (`runnable: false`) is still listed and still takes
  tasks. A run started for it is rejected by Exradius with a reason, which Desk shows as
  given.

### 2.1 People who can be assigned or mentioned

OpenID Connect offers no list of users, so Desk knows only the people who have signed in to
it. The people who can be assigned to a task, or mentioned in its comments, are those who
have **signed in to Desk at least once and are allowed on the task's project** by the
groups of their last sign-in. A person who left a group stays the assignee of what they
had, and is no longer offered.

## 3. Sign-in and sessions

OpenID Connect only, against **one issuer per install**: the authorization code flow with
PKCE (S256). There are no local accounts, passwords or invitations.

- **Discovery.** `<issuer>/.well-known/openid-configuration`, or `DESK_OIDC_DISCOVERY_URL`
  when Desk reaches the provider at another address than the browser does. In the local
  loop the browser uses `localhost` and Desk uses the cluster's service name (§14). The
  document is fetched at startup and kept for an hour; the JWKS is fetched again when a
  token names an unknown key id. A provider that cannot be reached at startup is a bridge
  problem (§11). Desk still starts, and sign-in answers 503 until discovery succeeds.
- **Login.** `GET /auth/login?next=<path>` stores `state`, `nonce`, the PKCE verifier and
  `next` in the `logins` table (rows expire after ten minutes) and redirects to the
  authorization endpoint with `response_type=code`, `scope` (`DESK_OIDC_SCOPES`, default
  `openid profile email`), `redirect_uri=<DESK_PUBLIC_URL>/auth/callback`, `state`,
  `nonce`, `code_challenge` and `code_challenge_method=S256`. `next` must be a path on
  Desk (it starts with a single `/`), and anything else becomes `/`.
- **Callback.** `GET /auth/callback?code&state`: the login row is used once, and the code
  is exchanged at the token endpoint with the verifier. The client authenticates with
  `client_secret_basic` when `DESK_OIDC_CLIENT_SECRET` is set (a confidential client), or
  sends only `client_id` otherwise (a public client). The ID token is verified: signature
  from the JWKS (RS256, ES256 and their larger variants; never `none` or HMAC), `iss` equal
  to `DESK_OIDC_ISSUER`, `aud` containing the client id, `exp` and `iat` with 60 seconds of
  leeway, and `nonce`. An error from the provider (`?error=access_denied`) or a failed check
  shows the sign-in page with the reason.
- **Who.** The person is identified by `sub`. Their name comes from `name`, else
  `preferred_username`, else `email`, else `sub`. Their address comes from `email`, which
  Desk uses only for notifications (§10). When `email_verified` is `false`, no mail is sent
  to that address.
- **Groups.** They come from the claim named by `DESK_OIDC_GROUPS_CLAIM` (default
  `groups`). A dotted name reads a nested claim (`realm_access.roles`). The claim may be a
  list of strings or a single string. If the ID token lacks it, Desk asks the userinfo
  endpoint once. Without it the person has no groups and sees no project. The groups are
  recorded on the person at each sign-in.
- **Sessions.** A random 32-byte token in the cookie `desk_session` (`HttpOnly`,
  `SameSite=Lax`, `Secure` when `DESK_PUBLIC_URL` is https, path `/`). Only its sha256 is
  stored. It lasts `DESK_SESSION_HOURS` (default 12) from sign-in, with no sliding renewal.
  `POST /auth/logout` deletes the session. Signing out of the identity provider itself
  (RP-initiated logout) is not in v1.
- **Requests that change state** (`POST`, `PATCH` under `/api`, and `/auth/logout`) are
  refused with `403` when they carry an `Origin` other than `DESK_PUBLIC_URL`'s. Together
  with `SameSite=Lax`, that is the protection against cross-site requests.

## 4. Data model

Desk owns its database: Postgres in production (a database of its own, never Exradius's),
SQLite in tests, and Alembic migrations, which `exradius-desk migrate` runs (the pod's init
container, §13).

```
meta          key (pk), value                       install = 4 random characters (§6)
people        id, subject (unique), name, email, email_verified, groups (json),
              first_seen_at, last_seen_at
sessions      token_hash (pk), person_id, created_at, expires_at
logins        state (pk), verifier, nonce, next, created_at
counters      project (pk), last                    per-project task numbers
tasks         id, project, number, title, description, status, reporter_id,
              assignee_kind (none | person | exradius), assignee_id, handed_over_by,
              runs, created_at, updated_at                 unique (project, number)
runs          id, task_id, seq, exradius_task_id (unique), state, agent, agent_name,
              routing (json), handed_over_by, started_by_comment_id, retries,
              created_at, ended_at, reason, summary, error, warnings (json),
              cancel_requested_by, cancel_requested_at
comments      id, task_id, author_kind (person | exradius), author_id, kind, body,
              data (json), run_id, request_id, answered_by_id, answers_request_id,
              started_run_id, created_at
mentions      comment_id, person_id (null for Exradius), exradius (bool)
attachments   id, task_id, comment_id (null: the task's own), name, content_type, size,
              data (blob), uploaded_by, created_at
artifacts     id, run_id, comment_id, name, content_type, url
history       id, task_id, person_id (null: Desk), field, old, new, created_at
answerers     task_id, person_id                    (pk: both) for §10
```

- **tasks.runs** counts the run numbers the task has used for ids (§6), retries
  included; **runs.seq** is the run's place among the task's runs, as people see it.
- **tasks.status** is `todo`, `in_progress` or `done`. **assignee_kind** with
  **assignee_id** is the assignee. **handed_over_by** is the person who handed the task to
  Exradius for the live run (by assigning or by a mention), and whom it goes back to.
- **A run is live** when its task's assignee is Exradius. Starting a run assigns the task
  to Exradius, and ending one assigns it back, so these are one fact.
- **runs.state** is `sent` (published, and Exradius has not yet said `queued`), then
  Exradius's states (`queued`, `preparing`, `running`, `waiting_for_input`, `resuming`),
  then a final one (`completed`, `failed`, `cancelled`, `rejected`).
- **comments.kind**: `comment` (a person's), `question` (the agent's, with `request_id`
  and `answered_by_id` once a comment answers it), `started` (Exradius names the agent
  routing picked), `result`, `rejected` and `cancel`. `data` holds what Exradius's
  comments carry besides text: a question's context, the agent and the guess of
  `started`, a result's state, error and warnings. The effect of a person's comment that
  mentions Exradius is stored on it: `answers_request_id` or `started_run_id` (§7), and
  the thread shows it from there.
- **history** records changes to the title, description, status and assignee, by whom,
  and the hand-backs Desk makes itself (`person_id` null). The task page lists it among
  the comments.
- **Attachments** are kept in the database, capped at `MAX_ATTACHMENT_MB` per upload
  (§8). Results' files are not copied: Desk keeps their signed URLs (`artifacts`) and
  streams them from Exradius on download (§8).

## 5. Tasks, statuses and assignment

- **Keys.** `<project slug>-<number>`, numbered per project from 1 (`plane-12`). The key
  is the task's address: `/tasks/plane-12`.
- **Creating** needs a project (picked from the person's projects), a title, and
  optionally a description, attachments and an assignee. The reporter is the creator, and
  the status starts as To do. Creating a task assigned to Exradius starts a run at once.
- **Editing.** Anyone allowed on the project may change the title, the description and
  the status, and add attachments to the task. Each change is in the history.
- **Statuses** are To do, In progress and Done, and people move a task between them
  freely. Desk itself moves a task from To do to In progress when a run starts, and never
  moves one to Done: a person decides that.
- **Assigning.** The choices are nobody, Exradius, and the people of §2.1. Assigning to a
  person who is not the one assigning sends them a mail (§10).
- **While a run is live** the assignee cannot be changed (`409`). Cancel is the way to
  stop it (§7.3). When the run ends, for whatever reason, the task is assigned back to
  `handed_over_by`. That hand-back sends no "assigned to you" mail: the run-ended mail
  already goes to that person.

## 6. Task ids sent to Exradius

Exradius rejects a task id it has seen before, forever and from any bridge. The Jira
bridge uses issue keys (`PLANE-101`, `PLANE-101-2`) and the GitHub bridge
`<repository>-<number>` with any repository name. So any id of the form `<name>-<digits>`
could one day be a GitHub id, and Desk's ids need a character that neither of them can
produce.

- When Desk's database is created, the first migration stores four random characters from
  `[a-z0-9]` as `meta.install`.
- A task's first run is `<key>+<install>` (`plane-12+k7q2`), and later runs are
  `<key>+<install>-<n>` (`plane-12+k7q2-2`, `-3`), numbered as the Jira bridge numbers
  reruns. Neither a Jira key nor a GitHub repository name can contain `+`. The install
  part keeps the ids unique after Desk's database is replaced, and git accepts `+` in
  branch names, which start with the task id, so the branch begins with the task's key.
- **Duplicate ids.** If a rejection's reason starts with `duplicate task_id`, Desk takes
  the next run number and publishes again, up to five times, without posting anything.
  This is the kit's rule (kit §7) and the same text match (integration-api.md §11).

## 7. Runs

### 7.1 Starting

There is one way to start a run, with two entrances: assigning the task to Exradius, or a
comment that mentions Exradius while no run is live. Both:

1. assign the task to Exradius, set `handed_over_by` to the person who did it, and move a
   task in To do to In progress (history rows for both);
2. create the run with the next id (§6) and state `sent`, and for a mention record
   `started_run_id` on the comment;
3. commit, then publish the task. Exradius's `queued` event can arrive at once and is
   matched by id, so the rows must exist first. If publishing fails, the run and the
   assignment are rolled back, and the person sees `503` ("Exradius's broker cannot be
   reached").

The task message:

| Field | Value |
|---|---|
| `task_id` | §6. |
| `project` | The task's project. |
| `agent` | Left out: Exradius routes. People never pick an agent. |
| `instructions` | The title, a blank line, then the description. |
| `context` | `{"task": {"key", "url", "project", "status", "reporter", "handed_over_by"}, "comments": [...]}`. `comments` holds every comment so far in order, Exradius's questions, results, rejections and stops included, each as `{"author", "at", "kind", "text"}`, with mentions as the person's display name in plain text (`@Ada Lovelace`). For a run started by a mention, that comment is last. `skipped_attachments` lists files that did not fit. |
| `attachments` | Inline base64: the starting comment's files first, then the task's and the other comments' files newest first, while the total stays within `MAX_ATTACHMENT_MB`. Results' files are named in the comments, not attached again. Names are made plain and unique (the kit's `safe_name` and `unique`). |
| `correlation` | `{"bridge": "desk", "instance": DESK_PUBLIC_URL, "install": "<install>", "task": "plane-12"}` |

### 7.2 Exradius's events

Desk consumes the kit's queue `exradius-bridge.desk.events` (prefetch 1, one retry, then
drop: kit §10). It ignores task events whose correlation is not `bridge: desk` with this
install, and events for ids it does not know. Each event is handled under its task's lock
and in one transaction. After the commit, browsers are told (§9) and mail is sent (§10).

| Event | Desk |
|---|---|
| status `queued` | Run `queued`; `agent` from the envelope, its name from the directory (kept, so a later rename does not change past tasks), `routing` from the payload. Comment `started` by Exradius: "**Scout** is working on this", plus "(a guess: Scout 59%, Developer 41%)" for a `decided` guess, as in kit §6.1. If someone asked to cancel while the run was `sent`, the cancel is published again now (§7.3). |
| status `preparing`, `running`, `resuming`, `waiting_for_input` | Run state only. The task page shows the live state. |
| status `rejected` | `duplicate task_id`: retry (§6). Otherwise the run ends `rejected`, with comment `rejected` giving the reason as given, and the task goes back (§5). Mail: "could not start". |
| status `cancelled`, `failed`, `completed` | Run state only; the result follows. |
| `info_request` | Comment `question` by Exradius: the question, then the agent's context when given. Mail: "asks a question". |
| `result` | The run ends with its state, summary, error and warnings. Comment `result` by Exradius, one `artifacts` row per file, and the task goes back (§5). Mail: "finished", "could not finish" or "was stopped". The transcript URL is not kept or shown: it works for anyone who has it, and Desk shows no transcripts. |

An event for an earlier run of the task (a late duplicate) updates that run only.

### 7.3 Answering and cancelling

- **Answering.** A comment that mentions Exradius while the live run waits
  (`waiting_for_input`) with an unanswered question answers the oldest unanswered
  question. Desk publishes the answer, with the comment's text (mentions as names) and its
  files, then stores the comment with `answers_request_id` and marks the question answered
  by its author. That is kit §8's order: Exradius keeps only the first answer to a
  request, so a retry after a failure cannot deliver two. A comment without the mention is
  never sent to the agent.
- **A mention while the run is working** (any live state other than waiting with an open
  question) is refused with `409` and the reason "Exradius is working on plane-12; it reads
  comments when it asks a question". The comment is not stored. The public contract has no
  way to reach a working run (§17). The mention list does not offer Exradius then.
- **Cancelling.** `POST /api/tasks/{key}/cancel` while a run is live. Desk publishes the
  public cancel (integration-api.md §10) with the reason `cancelled by <name> in Desk`,
  records `cancel_requested_by`, and posts comment `cancel` ("Ada asked Exradius to
  stop"). Exradius then sends status `cancelled` and the result, which ends the run and
  hands the task back. While the run is `sent` (the task waits in the broker for a worker
  slot), Exradius does not know the task yet and drops the cancel, so Desk publishes it
  again when `queued` arrives. A second cancel of the same run changes nothing.

## 8. Comments, mentions and files

- **Comments** are Markdown text with optional attachments, written by people allowed on
  the project. They cannot be edited or deleted in v1. Exradius's comments (questions,
  started, results, rejections, stops) are written by Desk from events.
- **Mentions.** Typing `@` in the comment box offers the people of §2.1 and, when it would
  do something (§7.1, §7.3), Exradius under the name the assignee list uses for it. A
  mention is stored as a reference, not as text: the body carries `@[person:<id>]` or
  `@[exradius]`, and `mentions` holds one row each. So a changed display name still
  renders correctly. The server checks each reference: a person who is not of §2.1 is
  `422`.
- **What a person's mention does:** one mail, "you were mentioned on plane-12" (§10).
  Mentioning yourself sends nothing. A mention alone does not add anyone to the people who
  get the run mails.
- **What an Exradius mention does** depends on the run: it answers the open question, or
  starts a run when none is live, or is refused while the run works (§7). It sends no mail.
  The comment shows what it did ("answered Scout's question", "started run 2"), from the
  stored state.
- **Uploads.** The task's files and comments' files are `multipart/form-data`. One upload
  may total at most `MAX_ATTACHMENT_MB` (default 10, the kit's default, which fits
  RabbitMQ's default message size after base64); a larger total is `413`.
- **Downloads of what people attached:** `GET /api/attachments/{id}`, from the database,
  only for people allowed on the task's project, as `Content-Disposition: attachment`.
- **Results' files:** `GET /api/artifacts/{id}`. Desk streams the file from Exradius's
  signed URL after checking access, so the URL never reaches the browser (it works for
  anyone who has it). The URLs in `task.result` are built from Exradius's public address,
  which a bridge in the cluster may not reach (in the local loop it is `localhost`). So
  Desk fetches the same signed path from `EXRADIUS_API_URL`, the address it already uses for
  the integration API: the URL's path from `/api/artifacts/` on, with its query. A URL
  without that path is fetched as given. See §17.

## 9. Live updates

The browser holds one server-sent event stream, `GET /api/stream`, authenticated by the
session cookie. When a task changes (an event, a comment, an assignment, an edit), Desk
sends `event: task` with `{"key": "plane-12", "project": "plane"}` to every stream whose
person may see that project. The browser then reads the task and the list again (TanStack
Query invalidation). It is notify, then fetch: nothing secret travels on the stream, and
access is checked on the read. A comment line every 25 s keeps proxies from closing the
stream. After a reconnect, the browser reads everything again.

The hub is in memory, so Desk runs as **one replica** in v1. More would need Postgres
`LISTEN/NOTIFY` between replicas (§16).

## 10. Email

Optional. Without `DESK_SMTP_HOST`, Desk sends nothing and works otherwise.

| Mail | To |
|---|---|
| `plane-12 Fix invoice totals: assigned to you` | The new assignee, when someone else assigns a task to them. Not on Desk's hand-back. |
| `plane-12 Fix invoice totals: you were mentioned` | Each person mentioned in a comment, except its author. The comment's text follows. |
| `plane-12 Fix invoice totals: Scout asks a question` | The run's participants. |
| `plane-12 Fix invoice totals: finished` (`could not finish`, `was stopped`, `could not start`) | The run's participants. |

- **A run's participants** are the person who handed the task to Exradius for that run,
  the task's reporter, and anyone who has answered a question on the task (`answerers`).
  A mention alone does not make someone a participant.
- **Never sent** to a person without an address, with `email_verified: false`, or whose
  groups at their last sign-in no longer allow the project.
- **What:** plain text, one message per recipient. The body is the question, summary,
  error or comment, cut at 2000 characters, with mentions as names, then the link
  `<DESK_PUBLIC_URL>/tasks/plane-12` (`#comment-<id>` for a mention).
- **How:** `smtplib` in a worker thread, after the commit, never blocking event handling.
  `DESK_SMTP_TLS` is `starttls` (default), `tls` or `none`. Credentials are optional. A
  failed send is logged and kept for 15 minutes among the reported problems (§11). It is
  not retried.

## 11. Bridge status

Through the kit (integration-api.md §7): at startup, after every directory fetch, when the
problems change, and every 5 minutes. The problems are:

- each project's `bridges/desk.yaml` problems (§2);
- `the identity provider at <issuer> cannot be reached: <why>` or `... is not usable:
  <why>` (no PKCE S256 support, a discovery document whose issuer differs);
- recent failures: mail that could not be sent, and events whose handling failed (kept for
  15 minutes, the kit's rule).

To share this logic, the kit gains a `StatusReporter`: the report's shape, the
`only_if_changed` check and the heartbeat. `Kit` uses it too, and its behaviour is
unchanged.

## 12. Configuration

Environment only. The kit's variables are `EXRADIUS_BROKER_URL`, `EXRADIUS_API_URL`,
`EXRADIUS_INTEGRATION_TOKEN` (a token for the bridge `desk`) and `MAX_ATTACHMENT_MB`.
Desk adds these:

| Variable | Meaning |
|---|---|
| `DESK_DATABASE_URL` | `postgresql://...` (CloudNativePG's `uri`; Desk uses asyncpg) or `sqlite+aiosqlite:///...`. |
| `DESK_PUBLIC_URL` | `https://desk.example.com`: links in mail, the redirect URI, the `Origin` check, the correlation's `instance`. |
| `DESK_OIDC_ISSUER` | The `iss` of ID tokens. |
| `DESK_OIDC_DISCOVERY_URL` | Optional: where Desk reads the discovery document, when it reaches the provider at another address. |
| `DESK_OIDC_CLIENT_ID` | The client registered for `<DESK_PUBLIC_URL>/auth/callback`. |
| `DESK_OIDC_CLIENT_SECRET` | Optional: a confidential client's secret. |
| `DESK_OIDC_SCOPES` | Default `openid profile email`. Add the provider's scope for groups if it needs one. |
| `DESK_OIDC_GROUPS_CLAIM` | Default `groups`. |
| `DESK_SESSION_HOURS` | Default `12`. |
| `DESK_SMTP_HOST`, `DESK_SMTP_PORT` (587), `DESK_SMTP_TLS`, `DESK_SMTP_USERNAME`, `DESK_SMTP_PASSWORD`, `DESK_SMTP_FROM` | Optional mail (§10). `DESK_SMTP_FROM` is required with a host. |
| `EXRADIUS_BROKER_USERNAME`, `EXRADIUS_BROKER_PASSWORD` | Optional. They are put into `EXRADIUS_BROKER_URL`, URL-quoted, the way the chart hands the in-cluster broker's user to Exradius itself. |
| `PORT` | Default `8080`. |

Every problem is reported at once, and Desk does not start (as the Jira bridge does).

## 13. Deployment

- **Image** `exradius-desk`: built from `bridges/desk/Dockerfile` with the kit as a named
  build context (`docker build --build-context kit=../kit -t exradius-desk .`, kit §11). A
  Node stage builds the frontend, and the Python stage serves it with the API. The image
  runs as an unprivileged user, and the chart gives it a read-only root filesystem.
- **Chart** `charts/exradius`, values `desk.*`, off by default (`desk.enabled: false`). On:
  - a Deployment `exradius-desk` (one replica, §9), with the init container `migrate` and
    the container `desk` on port 8080, ready on `/healthz`;
  - its database: a CloudNativePG `Cluster` `exradius-desk-postgres` (database `desk`) by
    default, or `desk.database.existingSecret` with a URI. It is never Exradius's database;
  - environment from values, Secrets by reference only: `desk.integrationToken`, the OIDC
    client secret (`desk.oidc.existingSecret`), and the SMTP credentials
    (`desk.smtp.existingSecret`). The broker comes from the chart's own broker settings;
  - a Service, and an Ingress of its own when `desk.ingress.enabled` (Desk has its own host
    name, and the panel's Ingress is not shared);
  - the api's NetworkPolicy lets Desk reach port 8000, for the integration API and result
    files.
- The integration token is created in the panel (Settings, Integrations, bridge `desk`)
  and put in the Secret named by `desk.integrationToken`.

## 14. Local loop

- `make -C deploy/kind dev-desk` adds the **dev identity provider**: Keycloak in dev mode
  in the namespace `keycloak` (`deploy/kind/keycloak.yaml`, local only, like Vault's dev
  mode). It imports the realm `exradius` with:
  - the confidential client `exradius-desk` (redirect `http://localhost:3200/auth/callback`,
    a groups mapper, and the fixed local-only secret `desk-local-secret`);
  - the groups `team-a` and `team-b`;
  - three people: `ada` (password `ada`) and `ben` (password `ben`) in `team-a`, and `cem`
    (password `cem`) in `team-b`.

  It also writes the Secret `exradius-desk-oidc` in `exradius`. Keycloak's browser address
  is `http://localhost:8180` and its address in the cluster is
  `http://keycloak.keycloak.svc.cluster.local:8080`. Its backchannel endpoints follow the
  address asked (`KC_HOSTNAME_BACKCHANNEL_DYNAMIC`), and Desk reads discovery from the
  cluster address (`DESK_OIDC_DISCOVERY_URL`).
- Desk is on in `charts/exradius/values-local.yaml`, with the dev identity provider, so
  `tilt up` builds `exradius-desk` (a `custom_build`, since the kit is a named build
  context), creates its CloudNativePG database and forwards Desk to `localhost:3200`. A
  Tilt resource `keycloak` forwards Keycloak to `localhost:8180` once it exists.
- `make -C deploy/kind desk-token` signs in to the local API as the admin, creates an
  integration token for the bridge `desk`, stores it in the Secret `exradius-desk`, and
  restarts Desk. Until then Desk stops at startup, saying the token is missing. The chart
  marks that Secret optional, so the pod starts as soon as the Secret exists.
- A project shows in Desk once its config repository has `bridges/desk.yaml` with
  `groups: [team-a]`.

## 15. Package and tests

```
bridges/desk/
  pyproject.toml, uv.lock, Dockerfile, README.md, docs/design.md
  exradius_desk/
    main.py        the command: serve, migrate
    settings.py    environment (§12)
    app.py         FastAPI: routes, the static frontend, the Origin check
    auth.py        OIDC client and sessions (§3)
    access.py      bridges/desk.yaml and who sees what (§2)
    models.py      SQLAlchemy models (§4); migrations/ with Alembic
    desk.py        tasks, assignment, runs and Exradius's events (§5–§7)
    mentions.py    mentions as references, and as plain text (§8)
    files.py       where result files are fetched from (§8)
    db.py          migrations under an advisory lock
    live.py        the stream hub (§9)
    mail.py        notifications (§10)
  web/             the frontend (React 19, TypeScript, Vite, Tailwind 4, TanStack)
  tests/
```

Tests run against the kit's `FakeBroker` and `FakeExradius`, a fake OIDC issuer (an httpx
`MockTransport` that signs ID tokens with a key of its own), and a small SMTP server on a
local port:

- sign-in: the full code flow with PKCE, a bad state, a wrong nonce, issuer or audience, an
  expired token, groups from a nested claim and from userinfo, a provider that answers
  `error`, sessions that expire, logout;
- access: `bridges/desk.yaml` validation and problems, projects by group, a task of a
  project the person may not see (`404`), a broken file hiding its project, who can be
  assigned and mentioned;
- tasks: keys per project, edits and their history, statuses, assignment to a person and
  its mail, the `409` while a run is live;
- runs: assigning to Exradius (the task message without `agent`, the ids of §6, To do to
  In progress), a mention starting a run, `queued` with a guess, a question, an answer by
  mention (first wins), a refused mention while working, cancel in `sent` and while
  running, results in each state with files and the hand-back, a rejection, the
  duplicate-id retry, a rerun with the comments so far and the attachment cap, events for
  other bridges and installs ignored;
- result files streamed through `EXRADIUS_API_URL`;
- mail: each kind, to the right people, none without SMTP, a failed send reported;
- the stream hub's per-project fan-out;
- the Origin check and the `413` cap;
- settings: every problem at once;
- one test against a real RabbitMQ when `DESK_TEST_BROKER_URL` is set.

The frontend has its own lint, tests (vitest, jsdom) and build.

## 16. Not in v1

- Boards, sprints, epics, subtasks, labels, priorities, due dates, custom fields, custom
  workflows (statuses are fixed), watchers, time tracking, search beyond the list's
  filters (project, assignee, status), importing from other trackers.
- Editing or deleting comments; deleting tasks or attachments.
- Picking an agent.
- Local accounts, invitations, more than one issuer, signing out of the identity provider.
- Groups changed at the identity provider taking effect before the next sign-in.
- More than one replica (the stream hub, §9).
- Notifications other than email; per-person notification settings.
- Transcripts, logs, the hosts a run reached.
- Results' files attached again to later runs.
- Texts in other languages.

## 17. Open points in the contract

- **Messages to a working run.** The public contract reaches a run only through the answer
  to a question. A comment that mentions Exradius while its run is working therefore has
  nowhere to go, and Desk refuses it (§7.3). An inbound message that the runner delivers at
  the next turn would let people steer a run.
- **Result URLs and the address of the API.** `task.result` gives absolute URLs on
  Exradius's public address. A bridge inside the cluster may not reach that address, so
  Desk replaces the URL's base with `EXRADIUS_API_URL` for paths under `/api/artifacts/`
  (§8). This relies on the shape of a URL that the contract shows only as an example. A
  result that also carried each file's path, or a documented download route on the
  integration API, would remove the rewrite.
- **Reason codes.** Duplicate ids are recognised by the text of the rejection's reason, as
  in the kit (integration-api.md §11).
- **Cancel before intake.** The public cancel does nothing for a task Exradius has not
  accepted yet (§7.3), so Desk re-sends it on `queued`. An Exradius-side record of early
  cancels would make that unnecessary, but it would also let a cancel reserve task ids that
  were never sent.
- **Task id characters.** Nothing in the contract says which characters a task id may
  hold. Desk relies on `+` being accepted (it is today: any string up to 255 characters),
  and on other bridges never producing it.
