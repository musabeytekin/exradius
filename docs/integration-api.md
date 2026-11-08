# Integration API: config hook, directory, bridges

Status: agreed 2026-11-08, while designing the Jira bridge. Revised the same day after the
first live Jira trial: people no longer pick agents (`agent-routing.md`), so the directory
now tells each bridge which projects it serves and how, instead of offering agents to
choose from. Companion to `architecture.md` §4. Internal design note.

## 1. Why

An integration (a **bridge**: Jira, GitHub, ...) needs to know which Exradius project a
piece of work belongs to. A Jira issue in the space PLANE goes to the project `plane`; that
mapping is the bridge's configuration, and it changes with the project.

So it lives with the project: in the project's config repository, in a file of the
bridge's own, `bridges/<name>.yaml`, next to `exradius.yaml` but not in it.
`exradius.yaml` holds Exradius's configuration only. Exradius reads the bridge files with
the config and passes them through without interpreting them (§6).

Config repositories change at any time, and Exradius reads them only on demand: on a panel
re-read or PATCH, on Verify, at task intake and `preparing`. Four additions keep bridges
current:

1. **Config hook** (§2): the git host calls a per-project URL on push, and Exradius
   re-reads the config at once.
2. **Directory notice** (§3): a `directory.changed` event, sent only when the directory
   changes.
3. **Directory** (§4, §5): the projects a bridge serves, with its bridge file, fetched over
   HTTP with an integration token bound to that bridge.
4. **Bridge status** (§7): the bridge reports the problems it found in its configuration,
   and the panel shows them.

The pattern is notify, then fetch: the event only says that something changed, and the
data comes from a request. There are no periodic events, because the directory changes
rarely and a bridge fetches it when it starts anyway.

Exradius assumes the URLs it is given are reachable. Making them reachable is an
installation matter (a tunnel, a reverse proxy, firewall rules), not something Exradius
writes code for. That covers a git host that cannot reach the hook and a bridge that cannot
reach the API. So there is no polling fallback. The two public prefixes, `/api/hooks/` and
`/api/integration/`, are chosen so a reverse proxy can expose them without exposing the
admin API.

## 2. Config hook

- Each project gets a hook token when it is created: 32 random bytes, URL-safe base64,
  unique. The URL is `<EXRADIUS_PUBLIC_API_URL>/api/hooks/config/<token>`.
- `POST` with any body and any content type. Exradius ignores the body and the headers,
  and checks no host-specific signatures. The token identifies the project, and the hook
  does one thing: it re-reads the config at the branch head, fetched from the git host by
  Exradius itself. So the hook works with GitHub, GitLab, Bitbucket, Gitea or a `curl` in
  CI, and a forged or replayed call can only cause an extra re-read.
- The hook answers `202` before reading, and `404` for an unknown token. The re-read runs
  in the background in the API process, one per project at a time. Calls that arrive while
  a read is running mark one more read to run after it, so a burst of pushes causes at most
  two reads.
- The re-read is the panel's (`_reread` in `api/config_routes.py`): the snapshot is
  stored, a new commit requests a verification, and the directory check (§3) runs.
- The token is stored as is, on `projects.hook_token`. It grants nothing but a re-read, so
  it is not a secret in the `crypto.py` sense, and the panel shows it the way it shows the
  config URL. `POST /api/projects/{id}/hook-token` (admin) replaces it, and the old URL
  stops working.
- `projects.hook_last_at` records the last accepted call.
- Panel, project page, config tab: the hook URL with a copy button, `Last delivery: <time>`
  or `never`, and a Replace action. There are no instructions for registering it with a
  git host, because the panel shows state, not help.

Whoever owns the config repository registers the hook. The `exradius-project` skill can do
it later when it creates the config repository, if it is given the URL (§10).

## 3. Directory notice

The directory (§4) changes when one of these happens:

- a config read gives a different set of agents or repositories, different names or
  repository URLs, a different runnable state, or different bridge files (added, removed,
  changed, or newly broken). Reads happen on panel re-read and PATCH, the hook, task
  intake, `preparing` and Verify;
- a project is added or deleted;
- store keys are set or deleted, or a store is deleted, since that changes which projects
  are runnable.

A commit that changes anything else does not change the directory, so nothing is sent.
That covers a Dockerfile, a service, an agent's role, a repository's branch or the commit
id itself.

**Detection.** A single-row table `directory_state` holds `directory` (the full directory:
every project with all its bridge files, §4) as last published, and `published_at`. After
any of the writes above, the process that made it (API or worker) does the check:

1. Build the full directory.
2. Lock the row and compare.
3. If the directory differs, publish the notice, then store the new directory.

The API also runs the check once at startup. If publishing fails, the new directory is not
stored, so the next check publishes again. Two processes racing may both publish; that is
harmless, because the notice carries no data. Notices are not stored in `events`, since
that table and the panel's event lists are per task, and a directory notice belongs to no
task.

**The message.** It is the one event not tied to a task:

```json
{
  "version": 1,
  "type": "directory_changed",
  "timestamp": "2026-11-08T21:04:11Z",
  "payload": { "projects": ["plane"], "bridges": ["jira"] }
}
```

- Exchange `exradius.events`, routing key `directory.changed`. Integrations that bind
  `task.*` never see it; those that want it bind `directory.changed` too.
- `payload.projects` lists the slugs whose entry was added, changed or removed, computed
  from the stored previous directory. `payload.bridges` lists the bridges whose view
  (§4) changed. Both are informational: a bridge fetches its whole directory either way,
  and may skip a notice that does not name it.
- There is no `task_id`, `run_id`, `project`, `agent` or `correlation`. `DirectoryEvent`
  sits next to `EventEnvelope` in `schemas/messages.py`, and the routing key is in
  `ROUTING_KEYS`, so `GET /api/system/integration` lists it.

## 4. The directory

```
GET /api/integration/v1/directory
Authorization: Bearer <integration token>
```

The answer is the token's bridge's view: only the projects whose config repository has
that bridge's file, and only that file.

```json
{
  "bridge": "jira",
  "projects": [
    {
      "slug": "plane",
      "name": "Plane",
      "runnable": true,
      "repositories": [
        { "name": "plane", "url": "https://github.com/acme/plane.git" }
      ],
      "agents": [
        { "slug": "developer", "name": "Developer" },
        { "slug": "scout", "name": "Scout" }
      ],
      "bridge_config": { "projects": ["PLANE"] },
      "bridge_config_error": null
    },
    {
      "slug": "saleor",
      "name": "Saleor",
      "runnable": false,
      "repositories": [ ... ],
      "agents": [ ... ],
      "bridge_config": null,
      "bridge_config_error": "bridges/jira.yaml: not valid YAML: line 3: mapping values are not allowed here"
    }
  ]
}
```

- **Source.** The directory is built from the snapshots cached on `projects.snapshot`.
  Intake still reads the config afresh, so the directory serves the bridge's own
  decisions only. A task built from a stale directory is checked at intake like any other.
- **`runnable`.** It is the project health's `runnable`: no config errors and no missing
  secret keys. A project whose `exradius.yaml` has errors has no repositories or agents to
  list, so it appears with `repositories: []` and `agents: []`; its bridge file is still
  read and passed (§6). A bridge sends tasks for an unrunnable project anyway: the
  rejection's reason says precisely why, and the bridge shows it to the person.
- **`bridge_config`** is the bridge file, parsed from YAML and given as JSON, exactly as
  written. **`bridge_config_error`** is set instead when the file is broken (§6). The
  bridge validates the content itself and reports what it finds wrong (§7).
- **Repositories.** `name` and `url` of the config's `repositories`, so a bridge can find
  the project for a repository (a GitHub bridge, from an issue's repository). The URL is as
  written, except that any userinfo is removed as a safeguard
  (`https://user:token@host/...` becomes `https://host/...`). An scp-like `git@host:path`
  cannot carry a password and stays as written. Bridges normalise https and ssh forms
  themselves.
- **Agents.** Slugs and names, so a bridge can name the agent that routing picked
  (`agent-routing.md` §8). Roles are prompts and stay out, as do commits, which would make
  every commit a change.
- **Order.** Projects are sorted by slug, agents by slug within a project, and
  repositories keep the config's order, so equal directories compare equal.
- **Caching.** The response carries an `ETag` (sha256 of the body), and `If-None-Match`
  answers `304`.

## 5. Integration tokens

- The admin creates a token in the panel (Settings, Integrations, New token) with a
  **bridge** and a **name**. The bridge is a name like `jira`, `[a-z][a-z0-9-]*`, offered
  from the bridge files found in the config repositories, or typed for a bridge that no
  project uses yet. The name is a unique label such as `jira-prod`, defaulting to the
  bridge. Two tokens may share a bridge, so a token can be rotated without a gap.
- The token is `exr_` plus 32 random bytes in URL-safe base64, shown once. Only its sha256
  is stored, since the token is high-entropy and needs no slow hash:

  ```
  integration_tokens
    id, name (unique), bridge, token_hash (unique), created_at, last_used_at, revoked_at
  ```

- **Bound to its bridge.** A token sees only its bridge's directory (§4) and reports only
  its bridge's status (§7). Tokens have no other scopes: there is one company and one
  admin.
- Tokens are accepted only on `/api/integration/v1/*`, and those routes accept nothing
  else: not the admin session, not download signatures. Everything else a bridge does
  stays on the broker.
- Revoking a token takes effect immediately. `last_used_at` is written at most once a
  minute.
- The panel lists tokens (name, bridge, created, last used) and offers New and Revoke.
- **Migration.** Tokens created before bridges existed get `bridge` = their name when the
  name is a valid bridge name, and are revoked otherwise.

## 6. Bridge files

The rules are in `config-repo.md` §2.1. In short:

- `bridges/<name>.yaml` in the config file's directory, read at the same commit as the
  config, on every read.
- Exradius only checks that the file is YAML whose top level is a mapping, at most 64 KiB.
  It does not interpret the content, resolve `${secret:...}`, or know any bridge's keys.
- A broken file is a warning on the project, never an error: the project stays runnable,
  and the directory gives the error instead of the content.
- The snapshot keeps the files (`ProjectConfigSnapshot.bridges`: name to content or
  error), and the panel's Config tab lists them with their content.

## 7. Bridge status

A bridge reports what it found wrong in its configuration, so the admin sees it in the
panel instead of in a container log.

```
PUT /api/integration/v1/status
Authorization: Bearer <integration token>
```

```json
{
  "version": 1,
  "software": "exradius-jira 0.2.0 (exradius-bridge-kit 0.1.0)",
  "started_at": "2026-11-08T17:00:00Z",
  "problems": [
    { "project": "plane", "message": "bridges/jira.yaml: projects: PLANE is also claimed by bank" },
    { "project": null, "message": "Jira answered 401 for the bot account" }
  ]
}
```

- **Who.** The token says which bridge reports (decided: HTTP with the token, not the
  broker, because the broker cannot tell one sender from another).
- **What.** Each problem names the project it is about, or `null` for the bridge as a
  whole. An empty list means the bridge found nothing wrong. At most 100 problems, each
  message at most 1000 characters, the body at most 64 KiB; anything larger is `413`.
- **When.** The kit reports at startup, after every directory fetch, whenever its problems
  change, and every 5 minutes otherwise.
- **Storage.** The latest report per bridge replaces the previous one:

  ```
  bridge_status
    bridge (primary key), report (json), reported_at, token_id
  ```

- **Panel.** Settings, Integrations lists each bridge (from its tokens and its reports)
  with the last report's time and software, and its problems; a bridge whose last report
  is older than 15 minutes shows as "not reporting since <time>". The project page shows
  every bridge's problems about that project. Messages are shown as plain text.

## 8. Changes by area

Done before this revision (migration `0004`): the hook, `directory_state`,
`integration_tokens`, the directory and its notice. This revision adds:

- **Config reading:** bridge files in `config_repo.py` and the snapshot (§6).
- **Directory module** (`exradius/directory.py`): the full directory with every bridge's
  files; the per-bridge view; `payload.bridges`.
- **Models and migration** (`0005`, with the routing changes): `integration_tokens.bridge`,
  `bridge_status`.
- **API:** `PUT /api/integration/v1/status`; the token's bridge on create and list; the
  bridge names found in configs, for the New token form; bridge statuses for the panel
  (`GET /api/bridges`).
- **Frontend:** the New token form's bridge; Settings, Integrations with each bridge's
  status; bridge files and reported problems on the project page.

## 9. Tests

- The hook: `404` for an unknown token, and a burst of calls coalescing into at most two
  reads.
- The directory check: a commit that changes only a role sends nothing, a new agent sends
  one notice, a repository change sends one notice, filling a missing key flips `runnable`
  and sends one notice, and a changed bridge file sends one notice naming that bridge.
- The view: a token sees only projects with its bridge's file; a broken file gives
  `bridge_config_error`; a project with config errors still carries its bridge file.
- A failed publish is retried on the next check.
- Token auth: missing, wrong and revoked tokens are refused; the migration binds or revokes
  old tokens.
- `ETag` and `304`.
- Status: stored per bridge and replaced by the next report; limits answered with `413`.

## 10. Not in v1

- Verifying git host signatures on the hook (the token in the URL is the check).
- Registering the hook from the `exradius-project` skill.
- A public cancel message.
- Reason codes on rejections. Today a bridge that wants to retry a duplicate task id
  matches the text of the rejection's `reason` (`duplicate task_id '...'`).
- Secrets in bridge files.
