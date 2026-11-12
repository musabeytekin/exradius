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
3. per store: create the secret store a config names, allow the projects that may use it,
   and fill the keys the file references, or point the store at the Vault path that holds
   them (§7).

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
  memory: 6g                      # the workstation (setup, run); its request and limit

services:                         # dependencies started next to the agent, by host name on localhost
  postgres:
    image: postgres:16            # or dockerfile: + context: as above
    env:
      POSTGRES_USER: app
      POSTGRES_DB: app
      POSTGRES_PASSWORD: ${secret:DB_PASSWORD}
    ports: [5432]                 # documentation for the agent; unique across services
    ready: {command: [pg_isready, -U, app]}   # or {tcp: 5432}; default: image HEALTHCHECK or "running"
    memory: 512m                  # optional; its request and limit, like resources.memory
  minio:
    image: minio/minio
    command: [server, /data]      # optional; the container's arguments, the entrypoint is kept

env:                              # project environment, in the workstation and setup
  DATABASE_URL: postgres://app:${secret:DB_PASSWORD}@postgres:5432/app

network:                          # optional; what the agent may reach besides its model,
  allow:                          # repositories and connections (setup reaches any host)
    - repo.maven.apache.org
    - pypi.org
    - '*.pythonhosted.org'        # quoted: * starts a YAML alias

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
- Service names: `[a-z][a-z0-9-]*`, at most 55 characters (the sidecar is
  `service-<name>`), not `api`, `localhost`, `workstation`, anything starting with
  `exradius`, nor any repository folder name. Agent and connection names: `[a-z][a-z0-9-]*`.
- Services share the run's `localhost` (docs/environment-and-services.md §2), so a port is
  listed by one service only: `services.b.ports: 5432 is also a port of service a`.
- `resources.memory` and a service's `memory` are sizes with a binary unit (`512m`, `6g`,
  `1.5g`; at least `6m`). Each is the request and the limit of its containers: the run's pod
  is placed on a node with room for it, waiting as "waiting for cluster capacity" until one
  has, and a container over it is killed and fails with "exceeded its declared memory
  (6g)". Undeclared gets the install's default request and no limit
  (docs/run-environment.md §4).
- `workstation.cache` entries start with `~/` or `/`, name a directory below the home or
  the root (not `~`, `/tmp` or `/home/runner` themselves), use no `..`, lie outside `/work`
  and outside `/exradius`, `/opt/exradius`, `/proc`, `/sys`, `/dev`, `/etc/passwd` and
  `/etc/group`, and are listed once. Each is the directory `caches/<n>-<sanitised path>` of
  the project volume, mounted at its path in the setup containers and the workstation of
  tasks and Verify; nothing cleans them, and they are deleted with the project
  (docs/run-environment.md §5).
- A service's `command` is a non-empty list of strings (numbers quoted; `""` is an
  argument), the container's arguments; there is no `entrypoint` key.
- `network.allow` entries are host names (`pypi.org`), `*.` and a name for every name below
  it (`*.pythonhosted.org`, not `pythonhosted.org` itself), or IPv4 addresses; not URLs, not
  `host:port` (every port of an allowed host is allowed), not a bare `*`, not `localhost`
  (the run's own services need no entry), each listed once. During setup a run reaches any
  public host; in the agent phase only its model, its repositories, its connections (with
  their catalog hosts), the install's `egress.allow` and these. The environment section names
  the list; a refused host is a `403` naming it, and the task page lists it
  (docs/kubernetes.md §11).
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

Missing keys: `required_keys` minus the keys present in the named store (all of them when
the store does not exist or does not allow the project; none is known while the store
cannot be reached). A project with errors, a store it may not use or missing keys is **not
runnable**: tasks for it are rejected with the reason, the first that applies of:

| Reason | Panel health |
|---|---|
| `config error: <the first error>` (and what `capabilities.py` cannot run yet) | `config_errors` |
| `secret store bank-platform does not exist` | `missing_secrets` |
| `secret store bank-platform is not allowed for this project` (§7.1) | `store_not_allowed` |
| `secret store bank-platform is unreachable: <why>` (§7.3) | `store_unreachable` |
| `missing secret DB_PASSWORD in store bank-platform` / `missing secrets A, B in store …` | `missing_secrets` |

A config that names a store but references no secret needs neither the store nor its
permission. The same reasons fail a run in `preparing` and Verify's "Read config" step; a
missing key and an unreachable store never share words, so an operator can tell them apart
in the rejection, the run's error and the status events alike.

The project slug is the file's `project.slug`. The API refuses to add a config repository
whose slug collides with another project's, and a later change of slug in the file is
an error on that project until resolved (the row keeps the old slug).

## 4. Builds

`workstation.dockerfile` and `services.*.dockerfile` are built in the cluster
(`docs/kubernetes.md` §12) from the config repository at the run's commit, context limited
to the declared directory. Tag: `exradius/<project slug>/<workstation|service name>:<hash>`
under the registry prefix (`builds.registry`), where `hash` is the first 12 hex digits of
the git tree hash of the context directory at that commit (`git rev-parse <commit>:<context
path>`), or of `<tree hash>:<Dockerfile blob>` when the Dockerfile lies outside the context,
so an unchanged context never rebuilds and editing that Dockerfile does.

The worker turns each tag into a `Build` object, `build-<project>-<name>-<hash>`, created if
absent: concurrent runs and Verify wait on the same one. The operator runs it in a
single-use rootless BuildKit pod, which fetches the context (the `git archive` the worker
stored in the object store) from the api's internal port with the Build's token and pushes
the image and its layer cache (`<image>:buildcache`) to the registry. A succeeded Build means
the image exists; a failed one is replaced by the next run or Verify that needs the image.
Each Build is recorded in `image_builds` (`project_id`, `name`, `tree_hash`, `image`,
`state`, `started_at`, `ended_at`, `log_tail`, `build_object`, `digest`, `context_key`).

Verify builds every image first ("Build workstation", "Build service postgres") and shows
the build's log as it grows, or says the image is up to date; a run waits for its images in
`preparing` ("building images") and a run whose build fails ends `failed` in `preparing`
with the log tail in the error. Service Dockerfiles are built the same way, as
`exradius/<project>/<service>:<hash>`.

Images named with `image:` are pulled as today.

## 5. Connections and the catalog

The catalog moves from the panel to the backend (`exradius/catalog.py`): each entry has
a key, a name, a description, the transport, `command`/`args` or `url`, the config keys
it accepts (`required` and `optional`, each with a label and where it goes: an env
variable, a header with a format, or an argument), and `requires` (a launcher such as
`npx` or `uvx`, reported as a warning when the workstation is the default image), and
`hosts`: what it reaches besides its URL, which the agent phase allows (the package registry
the launcher downloads it from, the service's API; a config key marked `host`, such as
GitLab's `api_url` or Jira's `url`, adds its URL's host).
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

A store is a name the admin owns. A config names one (`secret_store`) and refers to its
keys (`${secret:NAME}`); whether the values are kept in Exradius or in an external vault is
the admin's knowledge only, so moving a project's values needs no commit in its config
repository. Revised 2026-11-11: stores are bound to projects, read through pluggable
backends, and the first external backend is HashiCorp Vault.

Tables: `secret_stores` (`id`, `name` unique `[a-z][a-z0-9-]*`, `kind` `internal` | `vault`,
`settings` JSON, `created_at`), `secret_store_entries` (an internal store's values:
`store_id`, `key`, `value_encrypted`, `updated_at`), `secret_store_projects` (`store_id`,
`project_id`, `allowed_at`) and `secret_store_changes` (§7.2). Values are write-only.

API: `GET /api/stores`, `POST /api/stores {name, kind?, settings?, allowed_projects?}`,
`GET /api/stores/{id}`, `PATCH /api/stores/{id} {allowed_projects?, kind?, settings?}`,
`DELETE /api/stores/{id}` (refused while a project's config names it),
`GET /api/stores/{id}/keys` (names only), `PUT /api/stores/{id}/keys` (several values),
`PUT /api/stores/{id}/keys/{key}`, `DELETE .../keys/{key}`, `GET /api/stores/{id}/changes`
and `GET /api/store-kinds`. Each project's view reports `required_keys` with `present` per
key against its store (`null` while the store cannot be reached), `store_allowed` and
`store_error`.

Runs resolve every reference from the store named in the file at run start. Provider
credentials and the git host credentials of §6 are unchanged (providers.md); they are the
admin's, not a project's, and always internal.

### 7.1 Who may use a store

The admin lists, per store, the projects allowed to use it. Before this, whoever could
commit to one project's config repository could name another team's store and bring its
keys into `env`, and setup reaches any public host.

- A new store allows no project (`allowed_projects` is empty unless given). "Create store"
  on a project's Secrets tab allows that project; elsewhere the panel offers the known
  projects to tick, never free text.
- A config naming a store that does not allow its project is not runnable: `secret store S
  is not allowed for this project`, health `store_not_allowed`. Such a project learns
  nothing about the store's keys.
- The check is wherever values are resolved, not only in the health:
  `project_state.store_values(session, backends, project_id, config)` is the one way to get
  values and always takes the project. Run creation fails, Verify fails, the run spec and
  the verify spec answer 422 and the egress proxy's policy 409 for a store the project may
  not use.
- A project removed from a store cannot start runs from then on. A run already started
  keeps what it resolved: its per-run Secret is written, and the egress proxy keeps the
  policy it has when a refresh is refused (kubernetes.md §11).
- Deleting a project removes it from every store, and each store's log says so.
- Migration `0009` allowed each existing project for the store its cached snapshot named,
  so no working install stopped running.

### 7.2 The change log

`secret_store_changes` is appended to and never updated: `at`, `actor` (the admin's user
name; `migration` for what `0009` recorded), `store_id` (no foreign key, so the log outlives
the store), `store` (its name then), `action` (`store_created`, `store_deleted`, `key_set`,
`key_deleted`, `project_allowed`, `project_removed`, `settings_changed`), `key`, `project`
(a slug) and `detail` (the kind and settings of a created store, the settings before and
after a change, why a key or project went). Never a value. The store's page shows it,
newest first (`GET /api/stores/{id}/changes`). Which keys a run resolved is not recorded:
`runs.config` and `runs.config_commit` determine them.

### 7.3 Backends

Every read of a store goes through a backend (`exradius/secret_backends`) that answers two
questions: which key names the store holds (`keys()`), and the values of some names
(`values(names)`). `project_state` asks for names in the health and for the names the
config references (its required keys, never the whole store) when values are resolved, and
hands `StoreValues` on: `resolve.py`, the per-run Secret, the Run CRD and the egress proxy's
injection do not know where a value came from.

- **Kinds.** `internal` (values encrypted with the master key in `secret_store_entries`, set
  through the API) and `vault` (§7.4). A store's `settings` say where an external store's
  values are, never how to sign in; they are validated per kind with strict pydantic models
  (`extra="forbid"`), as this file is (`settings.path: required`, `settings.mounts: unknown
  key`). `GET /api/store-kinds` lists the kinds, whether this install can use each (Vault
  needs its address in the values) and their settings fields, for the panel's forms.
- **External stores are read-only here.** Their key names come from the backend, and
  setting or deleting a key answers 409 (`store S reads its values from HashiCorp Vault; set
  and delete them there`): the vault is the only source of its values.
- **Moving a store.** `PATCH` with a new `kind` and `settings` changes the store in place,
  so the projects naming it follow without a commit. Leaving `internal` deletes the values
  kept here, each one logged as `key_deleted`; the panel asks for the store's name first.
- **Unreachable is not missing.** A backend that cannot answer raises `StoreUnreachable`:
  the reason is `secret store S is unreachable: <why>`, health `store_unreachable`, and the
  keys are neither present nor missing. Intake, `preparing`, Verify, the run spec and the
  egress policy let the backend try again for about ten seconds first; then the task is
  rejected, or the run fails, with that reason. The run and verify specs answer 503 (the
  runner retries), the egress policy 503 (the proxy keeps the policy it has). The panel and
  the directory ask once; the directory counts an unreachable store as runnable, since an
  outage is not the configuration and nothing would publish the directory again when it is
  over (integration-api.md §4).

### 7.4 HashiCorp Vault

One Vault per install, read by the api and the worker; a store of kind `vault` is one
secret of a KV version 2 engine, and the secret's fields are the store's keys.

- **Install settings** (values `vault.*`, `EXRADIUS_VAULT_*`): `address`, `authMount`
  (default `kubernetes`), `role` (default `exradius`), optionally `namespace` (Vault
  Enterprise, HCP Vault), `caBundle` (a ConfigMap) and `audience` (below). Without an
  address no store of kind `vault` can be made.
- **Store settings:** `mount` (default `secret`) and `path`, segments of letters, digits,
  `_`, `.`, `@` and `-`. The panel shows the store as `<mount>/<path>`.
- **Sign-in.** The api and the worker sign in as their ServiceAccounts (`exradius-api`,
  `exradius-worker`) through Vault's Kubernetes auth method, `POST
  /v1/auth/<authMount>/login {role, jwt}`. Exradius stores no Vault credential. The
  ServiceAccount token file is read again at every sign-in (the kubelet rotates it), and the
  Vault token is kept until a minute before its lease ends; a `403` signs in once more before
  it counts. With `vault.audience` set, the pods send a projected token for that audience
  only, and the role must name the same audience; without it, their own token, which the
  Kubernetes API accepts too.
- **Key names** come from `GET /v1/<mount>/subkeys/<path>?depth=1`, which lists the fields
  without reading a value (Vault 1.10 or later), and are kept 30 seconds so the panel and the
  directory do not call Vault on every view. A path that holds no secret (`404`) is a store
  with no keys, once the mount is known to be KV version 2 (below).
- **Values** come from `GET /v1/<mount>/data/<path>` when a run is created, a verification
  runs, the run or verify spec is served, or the egress proxy refreshes a policy. They are
  never cached. A field that is not a string fails with `secret store S: field N of
  <mount>/<path> in Vault is a number, not a string`; it is never converted.
- **Failures.** The network, a timeout (5 s a request), a 5xx (a sealed or standby Vault)
  or a 429 is tried again with a growing pause (1, 2, 4 s) for about ten seconds by the
  callers of §7.3 that retry; the panel's views try once and remember the failure for five
  seconds. A refused sign-in (a wrong role, an audience the role does not accept), a refused
  read (the policy) or a `404` that names an error (no KV version 2 at the mount, a Vault
  before 1.10) is not tried again; each becomes the `<why>` of `secret store S is
  unreachable: <why>`.
- **Vault's side**, for the installer: the Kubernetes auth method enabled at `authMount`, a
  role bound to both ServiceAccounts in the release namespace, and a policy that reads the
  stores' paths:

  ```hcl
  path "secret/data/teams/*"    { capabilities = ["read"] }
  path "secret/subkeys/teams/*" { capabilities = ["read"] }
  ```

- **Reach.** The api and the worker have no egress NetworkPolicy, so they reach Vault
  wherever it is; run pods never do.
- **Locally:** `make -C deploy/kind dev-vault`, then `tilt up -- --vault`, gives the kind
  cluster a dev-mode Vault set up as above with a sample secret (kubernetes.md §17).
- **KV version 2 only, and told apart.** On a KV version 1 mount, or any other engine, the
  `subkeys` and `data` paths answer `404` with no error, exactly like an empty KV version 2
  path (checked on Vault 2.2.2), so the store would look empty and its projects would read
  `missing secret …`. So when a read comes back as an empty path, the mount is looked up with
  `GET /v1/sys/internal/ui/mounts/<mount>/<path>` (`data.type`, `data.options.version`; the
  endpoint Vault's own CLI uses before a KV command, which the store's read policy is enough
  for), and anything but `kv` version 2 gives `secret store S is unreachable: kv1 is a KV
  version 1 mount; stores of kind vault read KV version 2 (vault kv enable-versioning kv1
  upgrades it)`. The answer is kept a minute per mount, and a read that finds the secret
  never asks. Saving a store's settings asks too, afresh, and refuses such a mount with 422
  (`settings.mount: …`); a Vault that cannot be reached, or a mount the role cannot see (403),
  does not stop the save, and the store then shows why.

## 8. Data model changes

Dropped tables: `repositories`, `env_vars`, `secrets`, `connections`, `agents`,
`agent_connections`, `project_agents`, `project_services`, `project_verifications`'
dependence on any of them. `projects` becomes: `id`, `slug`, `config_url`,
`config_branch` (nullable: remote default), `config_path` (nullable: search order of §2),
`snapshot`, `snapshot_commit`, `snapshot_read_at`, `created_at`. New: `git_credentials`,
`secret_stores`, `secret_store_entries`, `image_builds`; later (§7) `secret_store_projects`
and `secret_store_changes` (migration `0009`) and `secret_stores.settings` (`0010`). `tasks`/`runs` keep `project_id`
and the agent **slug** (string), plus `runs.config_commit` and a `runs.agent_snapshot`
JSON (name, model, provider, identity, connection names) for the transcript page.
`providers` and `provider_tests` stay; `agents.provider_id` is replaced by the config's
`provider` name (providers get a unique `name`, they already have one for display; make
it unique and slug-like).

The database and the data directory are wiped (no migration of existing rows). The
migration chain may be replaced by a single initial migration.

## 9. Worker changes

`preparing`: read the config at the branch head (§3) → reject/fail with the reason if not
runnable → build images (§4) → create the Run, whose pod (docs/kubernetes.md §7) clones the
`repositories` from their mirrors, starts the services as sidecars on its `localhost`
(environment-and-services.md §2), runs the setup commands and starts the workstation. The environment section (environment-and-
services.md §1) is generated from the snapshot: image name (or `built from
<dockerfile>`), repositories, services with their non-secret settings, notes. The
operating rules (architecture §13) are unchanged. The agent's `role` is the system
prompt's last part when present.

Verify: "Read config" (commit, errors, warnings, missing keys) → "Build …" → the verify Run's
steps: "Prepare workspace", "Clone …", "Start service …", "Setup: …", "List tools …"
(docs/setup-and-verify.md §1).

## 10. Admin panel

Pages: **Overview** (status strip; projects with their health: ok / config errors / store
not allowed / store unreachable / missing secrets; stores), **Projects** (list: slug, name, config repository, commit,
health; add = URL + optional branch and path; detail tabs: **Config** (the parsed file
rendered read-only in sections: repositories, workstation, services, env with secret
placeholders, connections, agents, notes; errors and warnings on top; the raw file in a
collapsible monospace block; "Re-read" button), **Secrets** (the store it names, each
required key present or missing, a fill control that writes to an internal store; for an
external one the same read-only, with where its values are; a store that does not allow the
project, or cannot be reached, says so), **Verify**, **Tasks**), **Stores** (list with where
each is kept, its keys, allowed projects and the projects naming it; per store: the allowed
projects with what each still lacks, its keys, add or replace a value of an internal store,
delete, its settings, and its change log; §7), **Providers**
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
  `missing_keys`, `runnable`, `errors`, `warnings`, `store_allowed`, `store_error`),
  `POST /api/projects/{id}/reread`,
  `PATCH /api/projects/{id}` (branch, path), `DELETE`.
- `GET /api/projects/{id}/agents/{slug}/resolved` → environment text, connection specs
  with placeholders, model, provider, identity.
- `GET /api/catalog`, `GET/POST/DELETE /api/git-credentials`, stores as in §7.
- Verification, tasks, events, providers, system: unchanged shapes except the fields
  above.

## 12. Out of scope

Local runs and a CLI; migration of existing data; pinning a config to a commit with admin
approval; a skill that writes the file (later, outside Exradius). External stores beyond
Vault (the clouds' secret managers) are in `ideas.md`.
