# Agent identity

Status: agreed design, 2026-11-06. Companion to `architecture.md`.

## 1. Why

Nothing in a run container says who the agent is. Git refuses to commit without a name
and email, so the agent invents one; commits carry no link to the task; and pushing
requires the agent to find a token in the project's secrets and wire it into the remote
itself, which sometimes writes the token into the clone's config. The identity belongs on
the agent, and applying it is the worker's job, not the prompt's.

## 2. The model

```
agents
  identity_name    display name; defaults to the agent's name
  identity_email   required; the user's choice (the panel suggests nothing, see onboarding §6)

repositories
  push_allowed     bool, default false
```

Identity is who the agent is when it acts. Credentials stay where they are: on
repositories (fetch and, with `push_allowed`, push) and on connections. There is no
separate identity entity.

## 3. What the worker does at container start

1. **Environment:** `GIT_AUTHOR_NAME`, `GIT_AUTHOR_EMAIL`, `GIT_COMMITTER_NAME`,
   `GIT_COMMITTER_EMAIL` from the agent's identity. Also `EXRADIUS_AGENT=<slug>` and
   `EXRADIUS_TASK_ID=<task id>` for the hook below (these two are part of the container's
   environment and therefore visible to the agent, which is fine).
2. **Git config in the run's home** (`$HOME/.gitconfig`, written into the state mount so it
   survives resume): `user.name`, `user.email`, `core.hooksPath=<state>/git-hooks`,
   `credential.helper` (see 4), `push.autoSetupRemote=true`, `init.defaultBranch=main`.
3. **Commit trailers:** `<state>/git-hooks/prepare-commit-msg` appends
   `Exradius-Task: <task id>` and `Exradius-Agent: <slug>` to every commit message unless
   already present. Hooks the repository itself defines are not run (hooksPath replaces
   them); that is acceptable in v1 and documented.
4. **No co-author line:** the Claude Code runtime writes `{"includeCoAuthoredBy": false}`
   into `settings.json` under `CLAUDE_CONFIG_DIR` before the session starts, unless that
   file exists. Commits then carry only the agent's identity and the two trailers above.
5. **The run user is `runner`:** setup and run containers get a generated `/etc/passwd` and
   `/etc/group` (read-only) mapping the run uid/gid to `runner` with home `/home/runner`,
   so tools that read the home from passwd agree with `$HOME` (runtime-bundles.md §2.4).

## 4. Push

- When a repository has `push_allowed` and an auth secret, the worker exports the secret as
  `EXRADIUS_GIT_CREDENTIAL_<N>` (N = repository index) and configures a credential helper
  for that remote's host that answers from the variable:
  `credential.<url>.helper=!f() { echo username=x-access-token; echo password=$EXRADIUS_GIT_CREDENTIAL_N; }; f`
  (username is the part before `:` when the secret is `user:token`). Nothing is written
  into the clone's `.git/config`.
- Without `push_allowed`, no helper is configured; a push fails with an authentication
  error, as now.
- Mirror fetches keep using the secret as before.

## 5. API and panel

- `AgentIn`/`AgentUpdate`/`AgentOut` gain `identity_name` (optional, defaults to name)
  and `identity_email` (required on create; migration backfills existing agents with
  `<slug>@exradius.invalid` and the panel flags those for editing).
- `RepositoryIn`/`RepositoryUpdate`/`RepositoryOut` gain `push_allowed`.
- Agent form: an Identity section with name (prefilled from the agent name) and email.
- Repository form: a "Allow push" toggle, enabled only when an auth secret is chosen, with
  help text "the agent can push to this remote with the selected credential".
- Task detail: commits are not tracked by Exradius; the trailers are the link from git
  history back to the task.
