# Projects as config repositories

Agreed 2026-11-07. Supersedes the panel-defined project (repositories, environment,
services, secrets, agents, connections and notes edited in the admin panel) and the
`.exradius` compose design that was reverted. Internal design for the implementation
sessions; Exradius itself carries no user documentation.

## 1. The model

A project is a git repository holding one file, `exradius.yaml`, written by the
project's developers. It defines everything about the project: name, repositories,
workstation, services, environment, agents, connections, notes, and the name of the
**secret store** its secrets come from. The admin's part is reduced to:

1. once: how Exradius reads config repositories (a credential per git host, §6);
2. per project: paste the config repository URL;
3. per store: create the secret store a config names, fill the keys the file references.

Exradius reads the file from the configured branch every time it needs it: on every run
(the run records the commit it used), on Verify, and on demand from the panel. The
repository is the only truth; nothing about a project is edited in the panel.

Agents belong to the project that defines them. There are no shared agents, no
connection objects, no project-scoped secrets. Tasks still name `project` and `agent`
slugs on the wire (architecture §4); the agent must exist in the project's current config
or the task is rejected.

## 2. The file

Location: `exradius.yaml` at the repository root; if absent, `.exradius/exradius.yaml`
(a config kept inside the project's own code repository). Branch: the project's
configured branch, defaulting to the remote's default branch.

```yaml
version: 1

project:
  slug: bank                      # [a-z][a-z0-9-]*, unique across Exradius; tasks name it
  name: Bank of Anthos
  description: Sample retail bank, Java and Python services.

secret_store: bank-platform       # every ${secret:NAME} below resolves from this store

repositories:                     # cloned into the project directory, one folder each
  - name: bank-of-anthos          # folder name
    url: https://github.com/acme/bank-of-anthos.git
    branch: main
    credential: ${secret:GITHUB_TOKEN}   # optional; token or user:password
    push: true                           # default false; needs credential

workstation:                      # the agent's container image; one of image / dockerfile
  image: bank-of-anthos-dev:latest
  # dockerfile: docker/workstation.Dockerfile   # path in the config repository
  # context: docker/                            # build context, default: the Dockerfile's directory
  setup:                          # run after cloning, in the workstation, project dir as cwd
    - cp -r /opt/m2 ~/.m2
  cache:                          # optional; kept across the project's runs, shared by them
    - ~/.m2/repository            # ~-relative or absolute, outside /work

resources:                        # optional
  memory: 6g                      # the workstation (setup, run, resume); reserved and enforced

services:                         # dependencies started next to the agent, by host name
  postgres:
    image: postgres:16            # or dockerfile: + context: as above
    env:
      POSTGRES_USER: app
      POSTGRES_DB: app
      POSTGRES_PASSWORD: ${secret:DB_PASSWORD}
    ports: [5432]                 # documentation for the agent; nothing is published
    ready: {command: [pg_isready, -U, app]}   # or {tcp: 5432}; default: image HEALTHCHECK or "running"
    memory: 512m                  # optional; reserved and enforced like resources.memory
  minio:
    image: minio/minio
    command: [server, /data]      # optional; the container command, the entrypoint is kept

env:                              # project environment, in the workstation and setup
  DATABASE_URL: postgres://app:${secret:DB_PASSWORD}@postgres:5432/app

notes: |                          # quoted in the environment section; optional
  Python services: uv run pytest in the service directory. Java: mvn test per module.

connections:                      # MCP servers agents may use; optional
  github:                         # catalog item: known transport, declared config keys
    catalog: github
    config: {token: "${secret:GITHUB_TOKEN}"}   # quoted: { } is YAML flow syntax
  internal-docs:                  # custom, mcpServers style (stdio)
    command: npx
    args: ["-y", "@acme/docs-mcp"]
    env: {DOCS_TOKEN: "${secret:DOCS_TOKEN}"}
  tracker:                        # custom, mcpServers style (http)
    url: https://mcp.example.com/mcp
    headers: {Authorization: "Bearer ${secret:TRACKER_TOKEN}"}

agents:                           # at least one
  developer:
    name: Developer               # default: the slug, capitalised
    role: |                       # optional; appended after the environment section
      You are a software developer on this team. ...
    model: claude-sonnet-5-5      # optional; default: the runtime's default
    provider: main                # optional; a provider name from the panel; default: the default provider
    runtime: claude_code          # optional; default claude_code
    identity: {name: Developer, email: dev-bot@acme.com}   # defaults: name; <agent>@<project>.exradius.local
    connections: [github]         # optional; names from `connections`
  scout:
    role: You investigate and report. You never change code.
```

Rules:

- `${secret:NAME}` may appear in any string value under `repositories[].credential`,
  `services.*.env`, `env`, `connections.*` (`config`, `env`, `headers`). Nowhere else.
  `NAME` matches `[A-Za-z0-9_.-]+`. The set of names referenced is the project's
  **required keys**; it is computed from the file, never declared.
- Secrets never appear in the environment section, in the panel, in events or in logs.
  Env values that contain a secret reference are shown as `$NAME`-style placeholders
  the way `environment.py` already does for service settings.
- `dockerfile` and `context` paths are relative to the config file's directory and must
  stay inside the repository (no `..`, no absolute paths, no symlinks leaving it).
- Service names: `[a-z][a-z0-9-]*`, not `api`, `exradius-api`, nor any repository folder
  name. Agent and connection names: `[a-z][a-z0-9-]*`.
- `resources.memory` and a service's `memory` are Docker sizes with a unit (`512m`, `6g`,
  `1.5g`; at least `6m`). Each is reserved before a run or verification starts (it waits
  in `queued` until the worker's budget fits) and is the container's memory limit, swap
  included; a container killed by it fails with "exceeded its declared memory (6g)".
  Undeclared reserves nothing and limits nothing (docs/run-environment.md §4).
- `workstation.cache` entries start with `~/` or `/`, name a directory below the home or
  the root (not `~`, `/tmp` or `/home/runner` themselves), use no `..`, lie outside `/work`
  and outside `/exradius`, `/opt/exradius`, `/proc`, `/sys`, `/dev`, `/etc/passwd` and
  `/etc/group`, and are listed once. Each is bind-mounted from
  `<data_dir>/caches/<slug>/<n>-<sanitised path>/` into the setup, run and resume
  containers and Verify; nothing cleans them, the project page shows their size, and they
  are deleted with the project (docs/run-environment.md §5).
- A service's `command` is a non-empty list of strings (numbers quoted; `""` is an
  argument); there is no `entrypoint` key.
- Unknown keys anywhere are errors (typos must not pass silently). `version` must be 1.

### 2.1 Bridge files

Added 2026-11-08 (`integration-api.md` §6). A bridge's configuration for this project, such
as the Jira spaces whose issues belong to it, lives in the config repository next to the
config file, in a file of its own. `exradius.yaml` stays Exradius's configuration only.

```
exradius.yaml
bridges/
  jira.yaml           # projects: [PLANE]
  github.yaml
```

- **Where.** `bridges/` in the config file's directory: the repository root for
  `exradius.yaml`, `.exradius/bridges/` for `.exradius/exradius.yaml`. Read at the same
  commit as the config, on every read.
- **Names.** `<name>.yaml` with `name` matching `[a-z][a-z0-9-]*`, the bridge's name (the
  one integration tokens are bound to). Any other entry in `bridges/` is ignored with a
  warning naming it.
- **Content.** YAML whose top level is a mapping, at most 64 KiB. Nothing else is checked:
  Exradius does not know any bridge's keys, does not resolve `${secret:...}` (secrets do
  not belong in these files) and passes the content to the bridge as written. Each bridge
  validates its own file and reports problems through its status (`integration-api.md` §7).
- **Problems are warnings.** A file that is not valid YAML, is not a mapping, or is too
  large is a warning on the project (`bridges/jira.yaml: not valid YAML: ...`), never an
  error: the project stays runnable. Bridge files are read even when `exradius.yaml` has
  errors.

## 3. Reading, validating, snapshotting

`exradius/config_repo.py` (backend) does one thing: given a project row, refresh the
config repository's mirror with the host credential (§6), resolve the branch head,
read the file at that commit, parse YAML, validate against the schema above (pydantic
models in `exradius/schemas/project_config.py`, strict, `extra="forbid"`), resolve
catalog connections (§5) and return a **snapshot**:

```
ProjectConfigSnapshot:
  commit: str
  read_at: datetime
  config: ProjectConfig | None       # None when errors
  errors: list[str]                  # schema and rule violations, with the YAML path
  warnings: list[str]
  required_keys: list[str]           # secret names referenced
  store: str | None
  bridges: dict[str, BridgeFile]     # §2.1: name -> {config} or {error}
```

The API stores the latest snapshot on the project (`projects.snapshot` JSON,
`projects.snapshot_commit`, `projects.snapshot_read_at`) for the panel; the worker never
uses that cache for a run: it reads afresh in `preparing` and records `runs.config_commit`.

Missing keys: `required_keys` minus the keys present in the named store (or all of them
when the store does not exist). A project with errors or missing keys is **not runnable**:
tasks for it are rejected with the reason (`config error: ...` / `missing secret
DB_PASSWORD in store bank-platform` / `secret store bank-platform does not exist`).

The project slug is the file's `project.slug`. The API refuses to add a config repository
whose slug collides with another project's, and a later change of slug in the file is
an error on that project until resolved (the row keeps the old slug).

## 4. Builds

`workstation.dockerfile` and `services.*.dockerfile` are built by the worker with the
Docker SDK from a checkout of the config repository at the run's commit, context limited
to the declared directory. Tag: `exradius/<project slug>/<workstation|service name>:<hash>`
where `hash` is the git tree hash of the context directory at that commit
(`git rev-parse <commit>:<context path>`), so unchanged contexts never rebuild.
Builds are serialised per project with a file lock under the data directory and
recorded in `image_builds` (`project_id`, `name`, `tree_hash`, `image`, `state`,
`started_at`, `ended_at`, `log_tail`). Verify builds every image first ("Build
workstation", "Build service postgres") and shows the log tail; a run whose build fails
ends `failed` in `preparing` with the log tail in the error.

Images named with `image:` are pulled as today.

## 5. Connections and the catalog

The catalog moves from the panel to the backend (`exradius/catalog.py`): each entry has
a key, a name, a description, the transport, `command`/`args` or `url`, the config keys
it accepts (`required` and `optional`, each with a label and where it goes: an env
variable, a header with a format, or an argument), and `requires` (a launcher such as
`npx` or `uvx`, reported as a warning when the workstation is the default image).
Entries: github, gitlab, postgres, slack, sentry, linear, jira, fetch, playwright,
filesystem (`/work` default). A catalog connection with a missing required key, or an
unknown key, is a validation error naming the connection and the key.

A custom connection is the `mcpServers` shape: `command` + `args` + `env` (stdio) or
`url` + `headers` (http); `type` is accepted and ignored. Exactly one of `catalog`,
`command`, `url` must be present.

Resolution to the runner-facing spec (`exradius.resolve.connection_spec`) happens in the
worker at run start with secrets from the store; the panel's resolved view shows the
spec with secrets as `$NAME` placeholders.

`GET /api/catalog` lists the entries for the panel's read-only rendering (no forms).

## 6. Config repository access

`git_credentials` table: `host` (e.g. `github.com`), `kind` (`token` | `basic`),
`value_encrypted`, `created_at`. Settings page lists hosts; values are write-only.
Reading a config repository on host H uses the credential for H when present, else
anonymous. Code repositories inside the file use their own `credential` from the store;
they never fall back to the host credential.

## 7. Secret stores

`secret_stores` (`id`, `name` unique `[a-z][a-z0-9-]*`, `kind` = `internal`, `created_at`)
and `secret_store_entries` (`store_id`, `key`, `value_encrypted`, `updated_at`). Values are
write-only. API: `GET/POST /api/stores`, `DELETE /api/stores/{id}` (refused while a
project's config names it), `GET /api/stores/{id}/keys` (names only), `PUT
/api/stores/{id}/keys/{key}` (set), `DELETE .../keys/{key}`. Each project's view reports
`required_keys` with `present: bool` per key against its store.

Runs resolve every reference from the store named in the file at run start. Provider
credentials are unchanged (providers.md); they are the admin's, not the project's.

## 8. Data model changes

Dropped tables: `repositories`, `env_vars`, `secrets`, `connections`, `agents`,
`agent_connections`, `project_agents`, `project_services`, `project_verifications`'
dependence on any of them. `projects` becomes: `id`, `slug`, `config_url`,
`config_branch` (nullable: remote default), `config_path` (nullable: search order of §2),
`snapshot`, `snapshot_commit`, `snapshot_read_at`, `created_at`. New: `git_credentials`,
`secret_stores`, `secret_store_entries`, `image_builds`. `tasks`/`runs` keep `project_id`
and the agent **slug** (string), plus `runs.config_commit` and a `runs.agent_snapshot`
JSON (name, model, provider, identity, connection names) for the transcript page.
`providers` and `provider_tests` stay; `agents.provider_id` is replaced by the config's
`provider` name (providers get a unique `name`, they already have one for display; make
it unique and slug-like).

The database and the data directory are wiped (no migration of existing rows). The
migration chain may be replaced by a single initial migration.

## 9. Worker changes

`preparing`: read the config at the branch head (§3) → reject/fail with the reason if not
runnable → build images (§4) → create the run directory, clone the `repositories` →
start services on the per-run network (environment-and-services.md §2 unchanged) →
setup commands → start the workstation. The environment section (environment-and-
services.md §1) is generated from the snapshot: image name (or `built from
<dockerfile>`), repositories, services with their non-secret settings, notes. The
operating rules (architecture §13) are unchanged. The agent's `role` is the system
prompt's last part when present.

Verify: "Read config" (commit, errors, warnings, missing keys) → "Build …" → the existing
steps.

## 10. Admin panel

Pages: **Overview** (status strip; projects with their health: ok / config errors /
missing secrets; stores), **Projects** (list: slug, name, config repository, commit,
health; add = URL + optional branch and path; detail tabs: **Config** (the parsed file
rendered read-only in sections: repositories, workstation, services, env with secret
placeholders, connections, agents, notes; errors and warnings on top; the raw file in a
collapsible monospace block; "Re-read" button), **Secrets** (the store it names, each
required key present or missing, a fill control that writes to the store), **Verify**,
**Tasks**), **Stores** (list; keys per store, add or replace a value, delete), **Providers**
(unchanged), **Tasks** (unchanged; the send form's agent list comes from the project's
snapshot), **Settings** (admin password; config repository access per host).

Removed: Agents, Connections, Secrets, Integration pages; every guide, template, catalog
form and help panel; the "Pick, don't type" machinery for project fields. The panel shows
state and errors; it does not teach.

## 11. API summary

- `POST /api/projects {config_url, config_branch?, config_path?}` → reads once, returns
  the project with its snapshot (422 with the errors when the file cannot be read at all;
  a readable file with validation errors still creates the project so the panel can show
  them). `GET /api/projects`, `GET /api/projects/{id}` (with `required_keys`,
  `missing_keys`, `runnable`, `errors`, `warnings`), `POST /api/projects/{id}/reread`,
  `PATCH /api/projects/{id}` (branch, path), `DELETE`.
- `GET /api/projects/{id}/agents/{slug}/resolved` → environment text, connection specs
  with placeholders, model, provider, identity.
- `GET /api/catalog`, `GET/POST/DELETE /api/git-credentials`, stores as in §7.
- Verification, tasks, events, providers, system: unchanged shapes except the fields
  above.

## 12. Out of scope

Local runs and a CLI; migration of existing data; external store kinds; pinning a config
to a commit with admin approval; a skill that writes the file (later, outside Exradius).
