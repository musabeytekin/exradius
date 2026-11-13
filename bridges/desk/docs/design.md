# Exradius Desk design

Status: agreed 2026-11-14. Internal design note. Exradius Desk (`exradius-desk`) is
Exradius's own task bridge. It builds on `exradius-bridge-kit` (its `docs/design.md`, cited
as "kit §n"). The Exradius side is `docs/architecture.md` §4 (messages),
`docs/integration-api.md` (directory, bridge status, cancel) and `docs/agent-routing.md`
in the Exradius repository.

## 1. What it is

The other bridges connect an outside tracker (Jira, GitHub) to Exradius. Desk has no
outside system behind it, so a team without Jira or GitHub can still hand work to agents.
It is a **thin request inbox**. A person signs in, picks a project, writes the work,
follows its status, answers the agent's questions, reads the result and downloads its
files. A request is a **thread**: the work, its status, the questions and answers, and the
result. After a result the person can **continue** the thread, which starts a new run.

It is not a tracker. It has no boards, sprints, custom statuses, labels, assignees,
priorities, due dates, search or editing of what was sent. The panel and Desk show state,
not help: Desk has no guides, tips or empty-state explanations.

Desk is three things in one image:

- a FastAPI service on the kit's broker and integration API clients, with its own
  database;
- a small React frontend (`bridges/desk/web`), built into the image and served by the
  same process;
- the chart component that runs it (§13).

Like every bridge, it speaks only the public contracts and imports nothing from
`backend/`:

- the broker: `exradius.tasks`, `exradius.answers` and `exradius.cancels` out, and
  `exradius.events` in;
- the integration API: the directory with `bridges/desk.yaml`, the `directory.changed`
  notice, and the bridge status report;
- the signed download URLs of `task.result`.

## 2. Which people see which project: `bridges/desk.yaml`

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
- A project whose file is broken or invalid appears to nobody until it is fixed. Its
  threads stay in Desk's database and come back with the file.
- **Visibility is per project.** Someone in at least one of a project's groups sees all of
  its threads, may start new ones, answer any question, continue a thread and cancel a run.
  Nobody else sees that the project exists.
- Access is decided on every request, from the latest directory and the groups recorded at
  the person's last sign-in (§3). Removing a group from the file takes effect with the next
  directory fetch. Removing a person from a group at the identity provider takes effect at
  their next sign-in.
- A project that is not runnable (`runnable: false` in the directory) is still listed, and
  tasks for it are still sent. Exradius rejects them with a reason, and Desk shows that
  reason to the person as given.

## 3. Sign-in and sessions

OpenID Connect only, against **one issuer per install**: the authorization code flow with
PKCE (S256). There are no local accounts, passwords or invitations. Whoever the identity
provider lets in and puts in a project's group can use that project.

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
- **Callback.** `GET /auth/callback?code&state`: the login row is taken (one use), the
  code is exchanged at the token endpoint with the verifier, and the client authenticates
  with `client_secret_basic` when `DESK_OIDC_CLIENT_SECRET` is set (a confidential
  client), or sends only `client_id` otherwise (a public client). The ID token is verified:
  signature from the JWKS (RS256, ES256 and their larger variants; never `none` or HMAC),
  `iss` equal to `DESK_OIDC_ISSUER`, `aud` containing the client id, `exp` and `iat` with 60
  seconds of leeway, and `nonce`. An error from the provider (`?error=access_denied`) or a
  failed check shows the sign-in page with the reason.
- **Who.** The person is identified by `sub`. Their name comes from `name`, else
  `preferred_username`, else `email`, else `sub`. Their address comes from `email`, which
  Desk uses only for notifications (§10). When `email_verified` is `false`, no mail is sent
  to that address.
- **Groups.** They come from the claim named by `DESK_OIDC_GROUPS_CLAIM` (default
  `groups`). A dotted name reads a nested claim (`realm_access.roles`). The claim may be a
  list of strings or a single string. If the ID token lacks it, Desk asks the userinfo
  endpoint once. Without it the person has no groups and sees no project. Desk records the
  groups on the person at each sign-in.
- **Sessions.** A random 32-byte token in the cookie `desk_session` (`HttpOnly`,
  `SameSite=Lax`, `Secure` when `DESK_PUBLIC_URL` is https, path `/`). Only its sha256 is
  stored. It lasts `DESK_SESSION_HOURS` (default 12) from sign-in, with no sliding renewal.
  `POST /auth/logout` deletes the session. Signing out of the identity provider itself
  (RP-initiated logout) is not in v1.
- **Requests that change state** (`POST` under `/api`, `/auth/logout`) are refused with
  `403` when they carry an `Origin` other than `DESK_PUBLIC_URL`'s. Together with
  `SameSite=Lax`, that is the protection against cross-site requests.

## 4. Data model

Desk owns its database: Postgres in production (a database of its own, never Exradius's),
SQLite in tests, and Alembic migrations, which `exradius-desk migrate` runs (the pod's init
container, §13).

```
meta           key (pk), value                      install = 4 random characters (§5)
people         id, subject (unique), name, email, email_verified, groups (json),
               first_seen_at, last_seen_at
sessions       token_hash (pk), person_id, created_at, expires_at
logins         state (pk), verifier, nonce, next, created_at
threads        number (pk, from 1), project, title, created_by, created_at, updated_at,
               state, attempts
runs           id, thread_number, seq, task_id (unique), state, agent, agent_name,
               routing (json), requested_by, created_at, ended_at, reason,
               summary, error, warnings (json), cancel_requested_by, cancel_requested_at
entries        id, thread_number, run_id, kind, person_id, body, data (json), created_at
attachments    id, entry_id, name, content_type, size, data (blob)
questions      request_id (pk), run_id, entry_id, asked_at, answered_at, answered_by
artifacts      id, run_id, name, content_type, url
participants   thread_number, person_id, since                 (pk: both)
```

- **threads.state** is the latest run's state, kept so the list can be filtered without a
  join. **threads.attempts** counts the task ids the thread has used (§5).
- **runs.state** is `sent` (published, and Exradius has not yet said `queued`), then
  Exradius's states (`queued`, `preparing`, `running`, `waiting_for_input`, `resuming`),
  then a final one (`completed`, `failed`, `cancelled`, `rejected`).
- **entries** are the thread's timeline (§7): `request`, `follow_up`, `started`,
  `question`, `answer`, `cancel`, `result` and `rejected`.
- **attachments** are kept in the database. They are capped at `MAX_ATTACHMENT_MB` per
  message (§8), which Postgres stores comfortably. Results' files are not copied: Desk
  keeps their signed URLs (`artifacts`) and streams them from Exradius on download (§8).

## 5. Task ids and continuing a thread

Exradius rejects a task id it has seen before, forever, even one whose run failed. Desk's
ids must therefore never repeat, including after Desk's database has been replaced.

- When Desk's database is created, the first migration stores four random characters
  from `[a-z0-9]` as `meta.install`.
- A thread's task ids are `desk-<install>-<number>` for its first attempt and
  `desk-<install>-<number>-<attempt>` after that (`desk-k7q2-12`, `desk-k7q2-12-2`). Branches
  start with the task id, so they say where the work came from.
- **Duplicate ids.** If a rejection's reason starts with `duplicate task_id`, Desk
  increments the attempt counter and publishes again under the next id, up to five times,
  without telling anyone. This is the kit's rule (kit §7) and the same text match
  (integration-api.md §11).
- **Continuing.** Once a thread's run has ended (completed, failed, cancelled or rejected),
  anyone allowed on the project may continue it with more text and files. That starts a new
  run under the next id. The task carries the new text as its instructions and the thread
  so far as context (§6). A run is never resumed from Desk: a question is the only way to
  give a running agent more input.

## 6. The task message

| Field | Value |
|---|---|
| `task_id` | §5. |
| `project` | The thread's project. |
| `agent` | Left out: Exradius routes (agent-routing.md). People never pick an agent. |
| `instructions` | First run: the title, a blank line, then the text. A follow-up: its text. |
| `context` | `{"thread": {"number", "title", "url", "requested_by"}}` on the first run. A follow-up adds `"so_far"`: every earlier entry in order as `{"kind", "author", "at", "text"}` (requests, follow-ups, questions, answers, results' summaries and errors, rejections, cancels), plus `"files"` naming earlier attachments and results' files. `skipped_attachments` lists files that did not fit. |
| `attachments` | Inline base64. First run: the request's files. Follow-up: its own files, then earlier ones newest first, while the total stays within `MAX_ATTACHMENT_MB`. Results' files are named in the context, not attached again. Names are made plain and unique (the kit's `safe_name` and `unique`). |
| `correlation` | `{"bridge": "desk", "instance": DESK_PUBLIC_URL, "install": "<install>", "thread": <number>}` |

The run row and the entry are committed before the task is published, because Exradius's
`queued` event can arrive at once and is matched by task id. If publishing fails, both are
deleted (and a new thread with them), and the person sees `503` ("Exradius's broker cannot
be reached").

## 7. The flow for each event

Desk consumes the kit's queue `exradius-bridge.desk.events` (prefetch 1, one retry, then
drop: kit §10). It ignores task events whose correlation is not `bridge: desk` with this
install, and events for task ids it does not know. Each event is handled under its
thread's lock and in one transaction. After the commit, the browser is told (§9) and mail
is sent (§10).

| Event | Desk |
|---|---|
| status `queued` | Run `queued`; `agent` from the envelope, its name from the directory (kept, so a later rename does not change past threads), `routing` from the payload. Entry `started`: "**Scout** took this", plus "(a guess: Scout 59%, Developer 41%)" for a `decided` guess, as in kit §6.1. If someone asked to cancel while the run was `sent`, the cancel is published again now (§7.1). |
| status `preparing`, `running`, `resuming`, `waiting_for_input` | Run and thread state only. No entry: the thread header shows the current state. |
| status `rejected` | `duplicate task_id`: retry (§5). Otherwise the run ends `rejected` with entry `rejected`, the reason as given. Mail: "could not start". |
| status `cancelled`, `failed`, `completed` | Run state only; the result follows. |
| `info_request` | A `questions` row and entry `question` (the question, and the agent's context when given). Mail: "asks a question". |
| `result` | The run ends with its state, summary, error and warnings. One `artifacts` row per file. Entry `result`. Mail: "finished", "could not finish" or "was stopped". The transcript URL is not kept or shown: it works for anyone who has it, and Desk shows no transcripts. |

The thread's state follows its latest run. An event for an earlier run of the thread (a
late duplicate) updates that run only.

### 7.1 Answers and cancel

- **Answering.** `POST /api/threads/{n}/answers` with `request_id`, the text and files.
  The question must be the run's and still open in Desk; otherwise `409` ("already answered
  by Ada"). The answer is published first and recorded after (kit §8's order): Exradius
  keeps only the first answer to a request, so a retry after a failure cannot deliver two.
  The answerer becomes a participant.
- **Cancelling.** `POST /api/threads/{n}/cancel` while the latest run has not ended. Desk
  publishes the public cancel (integration-api.md §10) with the reason `cancelled by <name>
  in Desk`, records `cancel_requested_by`, and adds entry `cancel`. Exradius then sends
  status `cancelled` and the result. If the run is still `sent` (the task waits in the
  broker for a worker slot), Exradius does not know the task yet and drops the cancel, so
  Desk publishes it again when `queued` arrives. A second cancel of the same run is
  accepted and changes nothing.

## 8. Attachments and files

- **Upload.** New requests, follow-ups and answers are `multipart/form-data`. The files of
  one message may total at most `MAX_ATTACHMENT_MB` (default 10, the kit's default, which
  fits RabbitMQ's default message size after base64); a larger total is `413`. Names are
  stored as given and made plain and unique when sent.
- **Downloads of what people attached:** `GET /api/attachments/{id}`, from the database,
  only for people allowed on the thread's project, as `Content-Disposition: attachment`.
- **Results' files:** `GET /api/artifacts/{id}`. Desk streams the file from Exradius's
  signed URL after checking access, so the URL never reaches the browser (it works for
  anyone who has it). The URLs in `task.result` are built from Exradius's public address,
  which a bridge in the cluster may not reach (in the local loop it is `localhost`). So
  Desk fetches the same signed path from `EXRADIUS_API_URL`, the address it already uses for
  the integration API: the URL's path from `/api/artifacts/` on, with its query. A URL
  without that path is fetched as given. See §17.

## 9. Live updates

The browser holds one server-sent event stream, `GET /api/stream`, authenticated by the
session cookie. When a thread changes (an event, an answer, a cancel, a follow-up), Desk
sends `event: thread` with `{"number": 12, "project": "plane"}` to every stream whose
person may see that project. The browser then reads the thread and the list again
(TanStack Query invalidation). It is notify, then fetch, so nothing secret travels on the
stream and access is checked on the read. A comment line every 25 s keeps proxies from
closing the stream. After a reconnect, the browser reads everything again.

The hub is in memory, so Desk runs as **one replica** in v1. More would need Postgres
`LISTEN/NOTIFY` between replicas (§16).

## 10. Email

Optional. Without `DESK_SMTP_HOST`, Desk sends nothing and works otherwise.

- **When:** only for "the agent asked a question" and "the run ended" (result, failure,
  stop, rejection).
- **To whom:** the thread's participants. These are the person who opened it, whoever
  continued it, and whoever answered a question. Excluded are people without an address,
  people whose address is not verified, and people whose groups at their last sign-in no
  longer allow the project. The person whose action ended the run (the canceller) is
  still told, since the stop is confirmed by Exradius later.
- **What:** plain text, one message per recipient. The subject is `#12 Fix invoice totals:
  Scout asks a question` (or `finished`, `could not finish`, `was stopped`, `could not
  start`). The body holds the question, or the summary or error, cut at 2000 characters,
  then the link `<DESK_PUBLIC_URL>/threads/12`.
- **How:** `smtplib` in a worker thread, after the event's commit, never blocking event
  handling. `DESK_SMTP_TLS` is `starttls` (default), `tls` or `none`. Credentials are
  optional. A failed send is logged and kept for 15 minutes among the reported problems
  (§11). It is not retried.

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
  build context (`docker build --build-context kit=../kit -t exradius-desk .`, as kit §11
  describes). A Node stage builds the frontend, and the Python stage serves it with the
  API. The image runs as an unprivileged user with a read-only root filesystem.
- **Chart** `charts/exradius`, values `desk.*`, off by default (`desk.enabled: false`). On:
  - a Deployment `exradius-desk` (one replica, §9), with the init container `migrate` and
    the container `desk` on port 8080, readiness on `/healthz`;
  - its database: a CloudNativePG `Cluster` `exradius-desk-postgres` (database `desk`) by
    default, or `desk.database.existingSecret` with a URI. It is never Exradius's database;
  - environment from values, Secrets by reference only: `desk.integrationToken`, the OIDC
    client secret (`desk.oidc.existingSecret`), and the SMTP credentials
    (`desk.smtp.existingSecret`). The broker comes from the chart's own broker settings;
  - a Service, and an Ingress of its own when `desk.ingress.enabled` (Desk has its own host
    name; the panel's Ingress is not shared);
  - the api's NetworkPolicy lets Desk reach port 8000, for the integration API and result
    files.
- The integration token is created in the panel (Settings, Integrations, bridge `desk`)
  and put in the Secret named by `desk.integrationToken`.

## 14. Local loop

- `make -C deploy/kind dev-desk` adds the **dev identity provider**: Keycloak in dev mode
  in the namespace `keycloak` (`deploy/kind/keycloak.yaml`, local only, like Vault's dev
  mode). It imports a realm `exradius` with the confidential client `exradius-desk`
  (redirect `http://localhost:3200/auth/callback`, a groups mapper, and the fixed
  local-only secret `desk-local-secret`), the groups `team-a` and `team-b`, and two people:
  `ada` (password `ada`, in `team-a`) and `ben` (password `ben`, in `team-b`). It also writes
  the Secret `exradius-desk-oidc` in `exradius`. Keycloak's browser address is
  `http://localhost:8180` and its address in the cluster is
  `http://keycloak.keycloak.svc.cluster.local:8080`; its backchannel endpoints follow the
  address asked (`KC_HOSTNAME_BACKCHANNEL_DYNAMIC`), and Desk reads discovery from the
  cluster address (`DESK_OIDC_DISCOVERY_URL`).
- `tilt up -- --desk` installs with `deploy/kind/values-desk.yaml` (Desk on, its
  CloudNativePG database, the dev identity provider), builds `exradius-desk`, and forwards
  Desk to `localhost:3200` and Keycloak to `localhost:8180`.
- `make -C deploy/kind desk-token` signs in to the local API as the admin, creates an
  integration token for the bridge `desk`, stores it in the Secret `exradius-desk`, and
  restarts Desk. Until then Desk stops at startup, saying the token is missing.
- A project shows in Desk once its config repository has `bridges/desk.yaml` with
  `groups: [team-a]`.

## 15. Package and tests

```
bridges/desk/
  pyproject.toml, uv.lock, Dockerfile, README.md, docs/design.md
  exradius_desk/
    main.py        the command: serve, migrate
    settings.py    environment (§12)
    app.py         FastAPI: routes, static frontend, Origin check
    auth.py        OIDC client and sessions (§3)
    access.py      bridges/desk.yaml and who sees what (§2)
    models.py      SQLAlchemy models (§4); migrations/ with Alembic
    desk.py        the service: directory, publishing, events (§5–§7)
    files.py       attachments and result files (§8)
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
- access: `bridges/desk.yaml` validation and problems, projects by group, a thread of a
  project the person may not see (`404`), a broken file hiding its project;
- the flow: a new request with files, the task message without `agent`, `queued` with a
  guess, a question, an answer (first wins, `409` after), cancel in `sent` and while
  running, results in each state with files, a rejection, the duplicate-id retry, a
  follow-up with the thread as context and the attachment cap, events for other bridges
  and installs ignored;
- result files streamed through `EXRADIUS_API_URL`;
- mail to participants, none without SMTP, and a failed send reported;
- the stream hub's per-project fan-out;
- the Origin check and the `413` cap;
- settings: every problem at once;
- one test against a real RabbitMQ when `DESK_TEST_BROKER_URL` is set.

The frontend has its own lint, tests (vitest, jsdom) and build.

## 16. Not in v1

- Boards, sprints, statuses of Desk's own, labels, assignees, priorities, search.
- Editing or deleting what was sent; closing a thread (a thread whose run ended is simply
  done).
- Picking an agent.
- Local accounts, invitations, more than one issuer, signing out of the identity provider.
- Groups changed at the identity provider taking effect before the next sign-in.
- More than one replica (the stream hub, §9).
- Notifications other than email; per-person notification settings.
- Transcripts, logs, the hosts a run reached.
- Results' files attached again to follow-up runs.
- Texts in other languages.

## 17. Open points in the contract

- **Result URLs and the address of the API.** `task.result` gives absolute URLs on
  Exradius's public address. A bridge inside the cluster may not reach that address, so
  Desk replaces the URL's base with `EXRADIUS_API_URL` for paths under `/api/artifacts/`
  (§8). This relies on the shape of a URL that the contract shows only as an example. A
  result that also carried each file's path, or a documented download route on the
  integration API, would remove the rewrite.
- **Reason codes.** Duplicate ids are recognised by the text of the rejection's reason, as
  in the kit (integration-api.md §11).
- **Cancel before intake.** The public cancel does nothing for a task Exradius has not
  accepted yet (§7.1). Desk re-sends it on `queued`. An Exradius-side record of early
  cancels would make that unnecessary, but it would also make cancel a way to reserve
  task ids that were never sent.
