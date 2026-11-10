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
2. **Git config in the run's home** (`GIT_CONFIG_GLOBAL` in the state mount, so it
   survives resume): `user.name`, `user.email`, `push.autoSetupRemote=true`,
   `init.defaultBranch=main`. `core.hooksPath=<state>/git-hooks` and the credential helpers
   (see 4) are git command-line config (`GIT_CONFIG_COUNT`/`KEY_n`/`VALUE_n`), which no
   config file outranks (docs/run-environment.md §3).
3. **Commit trailers:** every hook name in `<state>/git-hooks/` is one dispatcher. In
   `prepare-commit-msg` it appends `Exradius-Task: <task id>` and `Exradius-Agent: <slug>`
   unless already present; then, for every hook, it runs the repository's own hook of that
   name (from the hooks path a config file sets, such as husky's `.husky/_`, else
   `.git/hooks`) with the same arguments and stdin, and exits with its status. A failing
   project hook fails the commit, as for a developer.
4. **No co-author line:** the Claude Code runtime passes `{"includeCoAuthoredBy": false,
   "attribution": {"commit": "", "pr": ""}}` as `--settings`, which outranks the
   repository's `.claude/settings.json`. Commits then carry only the agent's identity and
   the two trailers above.
5. **The run user is `runner`:** setup and run containers get a generated `/etc/passwd` and
   `/etc/group` (read-only) mapping the run uid/gid to `runner` with home `/home/runner`,
   so tools that read the home from passwd agree with `$HOME` (runtime-bundles.md §2.4).

## 4. Push

Since phase 4 of the move to Kubernetes (`kubernetes.md` §11) the credential never reaches
the pod:

- Each http(s) repository gets `url.<egress>/git/<host><path>.insteadOf=<url>` at the git
  command-line level, so the agent's fetch and push of `origin` go to the egress proxy's git
  route, which adds the repository's credential as HTTP Basic (username: the part before `:`
  of a `user:token` secret, else `x-access-token`). Nothing is written into the clone's
  `.git/config`.
- Without `push` (`push_allowed`) the proxy refuses the push itself; git prints `fatal:
  remote error: pushing to <url> is not allowed for this task (push: false)`.
- Clones and mirror fetches use the same route in their own containers.
- Until phase 4 the token went into `EXRADIUS_GIT_CREDENTIAL_<N>` with a command-line
  credential helper answering from it.

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
