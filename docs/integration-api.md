# Integration API: config hook, directory, directory notice

Status: agreed 2026-11-08, while designing the Jira bridge (`exradius-jira`). Companion to
`architecture.md` §4. Internal design note.

## 1. Why

An integration layer has to offer the people who assign work a choice of what Exradius can
run: its projects and their agents. The Jira bridge puts that choice in a Jira field. Today
the list exists only in the admin API, behind the panel's session.

The list also goes stale. Projects and agents are defined in config repositories that
change at any time, and Exradius reads them only on demand: on a panel re-read or PATCH,
on Verify, and at task intake and `preparing`. A pushed agent stays invisible until one of
those happens.

Three additions fix that:

1. **Config hook** (§2): the git host calls a per-project URL on push, and Exradius
   re-reads the config at once.
2. **Directory notice** (§3): a `directory.changed` event, sent only when the list of
   projects and agents changes.
3. **Directory** (§4, §5): the list itself, fetched over HTTP with an integration token.

The pattern is notify, then fetch: the event only says that something changed, and the
data comes from a request. There are no periodic events, because the directory changes
rarely and an integration fetches it when it starts anyway.

Exradius assumes the URLs it is given are reachable. Making them reachable is an
installation matter (a tunnel, a reverse proxy, firewall rules), not something Exradius
writes code for. That covers a git host that cannot reach the hook and an integration that
cannot reach the API. So there is no polling fallback. The two public prefixes,
`/api/hooks/` and `/api/integration/`, are chosen so a reverse proxy can expose them
without exposing the admin API.

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
it later when it creates the config repository, if it is given the URL (§7).

## 3. Directory notice

The directory (§4) changes when one of these happens:

- a config read gives a different set of agents, different names or a different runnable
  state. Reads happen on panel re-read and PATCH, the hook, task intake, `preparing` and
  Verify;
- a project is added or deleted;
- store keys are set or deleted, or a store is deleted, since that changes which projects
  are runnable.

A commit that changes anything else does not change the directory, so nothing is sent.
That covers a Dockerfile, a service, an agent's role or the commit id itself.

**Detection.** A single-row table `directory_state` holds `directory` (the JSON body of §4
as last published) and `published_at`. After any of the writes above, the process that made
it (API or worker) does the check:

1. Build the directory.
2. Lock the row and compare.
3. If the directory differs, publish the notice, then store the new directory.

The API also runs the check once at startup. If publishing fails, the new directory is not
stored, so the next check publishes again. Two processes racing may both publish; that is
harmless, because the notice carries no data. Notices are not stored in `events`, since
that table and the panel's event lists are per task, and a directory notice belongs to no
task.

**The message.** It is the first event not tied to a task:

```json
{
  "version": 1,
  "type": "directory_changed",
  "timestamp": "2026-11-08T21:04:11Z",
  "payload": { "projects": ["plane"] }
}
```

- Exchange `exradius.events`, routing key `directory.changed`. Integrations that bind
  `task.*` never see it; those that want it bind `directory.changed` too.
- `payload.projects` lists the slugs whose entry was added, changed or removed, computed
  from the stored previous directory. It is informational only: an integration fetches the
  whole directory either way.
- There is no `task_id`, `run_id`, `project`, `agent` or `correlation`. A new model,
  `DirectoryEvent`, sits next to `EventEnvelope` in `schemas/messages.py`, and the routing
  key is added to `ROUTING_KEYS`, so `GET /api/system/integration` lists it.

## 4. The directory

```
GET /api/integration/v1/directory
Authorization: Bearer <integration token>
```

```json
{
  "projects": [
    {
      "slug": "plane",
      "name": "Plane",
      "runnable": true,
      "agents": [
        { "slug": "developer", "name": "Developer" },
        { "slug": "scout", "name": "Scout" }
      ]
    },
    { "slug": "saleor", "name": "Saleor", "runnable": false, "agents": [ ... ] }
  ]
}
```

- **Source.** The directory is built from the snapshots cached on `projects.snapshot`.
  Intake still reads the config afresh, so the directory is only for offering choices. A
  task built from a stale directory is checked at intake like any other.
- **`runnable`.** It is the project health's `runnable`: no config errors and no missing
  secret keys. A project whose config has errors has no agents to list, so it appears with
  `agents: []`.
- **Names only.** Roles are prompts, so they stay out, as do commits, which would make
  every commit a change, and reasons, which the admin reads in the panel. An integration
  needs only what it offers and the slugs it sends back.
- **Order.** Projects are sorted by slug, and agents by slug within a project, so equal
  directories compare equal.
- **Caching.** The response carries an `ETag` (sha256 of the body), and `If-None-Match`
  answers `304`.

## 5. Integration tokens

- The admin creates a token in the panel (Settings, Integrations, New token) with a name
  such as `jira`. The token is `exr_` plus 32 random bytes in URL-safe base64, shown once.
- Only its sha256 is stored, since the token is high-entropy and needs no slow hash:

  ```
  integration_tokens
    id, name (unique), token_hash (unique), created_at, last_used_at, revoked_at
  ```

- Tokens are accepted only on `/api/integration/v1/*`, and those routes accept nothing
  else: not the admin session, not download signatures.
- In v1 a token can read the directory, nothing more. Everything else an integration does
  stays on the broker. Tokens have no scopes: there is one company and one admin, and any
  later integration route is open to every token.
- Revoking a token takes effect immediately. `last_used_at` is written at most once a
  minute.
- The panel lists tokens (name, created, last used) and offers New and Revoke.

## 6. Changes by area

- **Models and migration** (`0002`, on top of `0001`): `projects.hook_token`,
  `projects.hook_last_at`, `directory_state` and `integration_tokens`. Existing projects
  get a hook token in the migration.
- **Directory module** (new `exradius/directory.py`): build the directory, compare it,
  publish the notice. Its check is called after every write listed in §3:
  - `config_routes.py`: project create, re-read, PATCH and delete, store key set and
    delete, store delete;
  - the worker: intake and `preparing` (after `remember`) and Verify.
- **API.**
  - `api/hooks.py` serves `POST /api/hooks/config/{token}`.
  - `api/integration_routes.py` serves `GET /api/integration/v1/directory`.
  - Token management: `GET/POST /api/integration-tokens` and
    `DELETE /api/integration-tokens/{id}`.
  - `POST /api/projects/{id}/hook-token`.
  - `ProjectOut` gains `hook_url` and `hook_last_at`.
- **Messages.** `DirectoryEvent` and the `directory.changed` routing key.
- **Frontend.** The project page's config tab gets the hook URL, last delivery and
  Replace; Settings gets the integration tokens.
- **Tests.**
  - The hook: `404` for an unknown token, and a burst of calls coalescing into at most
    two reads.
  - The directory check: a commit that changes only a role sends nothing, a new agent
    sends one notice, and filling a missing key flips `runnable` and sends one notice.
  - A failed publish is retried on the next check.
  - Token auth: missing, wrong and revoked tokens are refused.
  - `ETag` and `304`.

## 7. Not in v1

- Scopes on integration tokens.
- Verifying git host signatures on the hook (the token in the URL is the check).
- Registering the hook from the `exradius-project` skill.
- A public cancel message (the Jira bridge decided to do without one in v1).
- Reason codes on rejections. Today an integration that wants to retry a duplicate task id
  matches the text of the rejection's `reason` (`duplicate task_id '...'`).
