# When Verify or a task fails

Start from the step that failed and its log, which the developer copies from the
project's Verify tab (or the task page). Exradius adds a one-sentence diagnosis when it
recognises the failure; treat it as the first hypothesis. Then reproduce locally before
changing anything: `check.sh` runs the same steps, and a fix that passes there is a fix.

Decide first whose problem it is. Three kinds of failure look alike:

- **the config** (the file, a Dockerfile, a setup command): fix it here;
- **the store** (a key missing, a token without access): the admin's, tell them which key
  and what it must be able to do;
- **the install** (no node large enough, a build profile missing on a node, a private
  address not allowed): the admin's, in the chart's values; nothing in the config
  repository changes it.

## By step

### Read config

| Log | Cause | Fix |
|---|---|---|
| `<path>: unknown key`, `must match ...`, `must be a string` | the file breaks the schema | `check.sh --validate` shows the same errors; schema.md has the rule |
| `secret_store: required, because the file references secrets` | a `${secret:...}` and no `secret_store` | name the store |
| "Secret store X does not exist", `missing secret NAME in store X` | the store or a key is not there yet | the admin creates the store and fills the key; Verify reruns by itself once the keys are in |
| `secrets are not allowed here` | a `${secret:...}` outside the allowed places | move it into `env` or the service's `env` and read it as `$NAME` |
| `<path>.dockerfile: ... does not exist in the repository` | the file is not committed, or the path is relative to the wrong directory | paths are relative to `exradius.yaml`'s own directory; commit and push |
| `bridges/jira.yaml: ...` (a warning) | the bridge's file is malformed | the project still runs; fix the file (schema.md, "Bridge files") |

### Build workstation, Build service *name*

| Log | Cause | Fix |
|---|---|---|
| a `RUN` step fails that passes locally | the build context differs: an untracked or ignored file, or another CPU architecture | `check.sh` warns about files Exradius will not see; `--platform linux/amd64` (or `arm64`) builds the cluster's architecture |
| `COPY` cannot find a file | it exists only on the developer's machine | commit it |
| a download in a `RUN` step is refused as "private or cluster-internal address" | builds go through the egress proxy | the admin's `egress.allowPrivate`, or fetch it in a setup command |
| the base image cannot be pulled | a private registry | the install's pull secret (admin), or a public base |
| "seccomp profile", "AppArmor profile", "rootlesskit" | the cluster's builder is not set up on its nodes | the install's, not the project's: pass the diagnosis to the admin |

### Prepare workspace, and a task that stays in "waiting for cluster capacity"

| Log | Cause | Fix |
|---|---|---|
| "No node has enough memory for the run's pod" | `resources.memory` plus the services' `memory` is more than any node has free | lower them to the measured peak plus headroom, or the admin adds a larger node |
| "No node can take the run's pod" | the install's node selector or tolerations | admin |
| "could not be pulled" | an `image:` the cluster's nodes cannot reach | the name and tag; a private registry needs the install's pull secret |

### Clone *repo*

| Log | Cause | Fix |
|---|---|---|
| "Git could not authenticate" | the token behind the repository's `credential` cannot read it | the store key: scope, expiry, the right owner. A repository never falls back to the credential Exradius reads config repositories with |
| "has no branch" | `branch` does not exist on the remote | the default branch's real name (`git remote show origin`) |
| "was not found" | a wrong `url`, or a private repository without a `credential` | the URL; add the credential |
| it hangs, then times out | the URL is `git@...` or `ssh://` | repositories must be `https://` |

### Start service *name*

| Log | Cause | Fix |
|---|---|---|
| "did not become ready within 120s waiting for port N" while the service's log says ready | it listens on 127.0.0.1 only; a `tcp` check connects to the run's own address | a `ready` `command`, or listen on all addresses |
| ready, then the first setup command gets "connection refused" | Postgres answers `pg_isready` on its socket during init | `pg_isready -h 127.0.0.1` |
| "exited with code N before it was ready", "Address already in use" | two services on one port, a missing variable, a bad `command` | the service's log tail says which; another port, the variable, the command |
| "exceeded its declared memory" / "ran out of memory" | the service's `memory` is too low, or none and the node is full | raise or declare it |

### Setup: *command*

| Log | Cause | Fix |
|---|---|---|
| `<tool>: not found` (exit 127) | the workstation image lacks it | the Dockerfile; the check's "Check workstation image" lists what is on PATH |
| a lockfile or `packageManager` error | the tool in the image is older or newer than the lockfile | pin the version the repository asks for (pitfalls.md) |
| "Read-only file system", "Permission denied" outside the home | the command installs into the image at run time | move it into the Dockerfile |
| exit 137, "exceeded the workstation's declared memory" | the step's peak is over `resources.memory` | raise it, or lower the step's parallelism |
| "connection timed out", "network is unreachable", a name that does not resolve | the tool ignores the proxy variables | give it its own proxy setting (network.md) |
| "resolves to a private or cluster-internal address" | an internal mirror or registry | the admin's `egress.allowPrivate` |
| a 401 or 403 from a package registry | a private registry without its token | a store key in `env` (network.md, "Private package registries") |

### List tools *connection*

| Log | Cause | Fix |
|---|---|---|
| `npx: not found`, `uvx: not found` | the connection's launcher is not in the workstation image | install Node or uv in the Dockerfile |
| "not on this task's network allowlist" | a custom stdio connection reaches a host nobody listed | `network.allow` |
| 401 from the server | the connection's token | the store key and its scopes |
| no answer within 50 seconds | the server needs something it does not have (a variable, a reachable host) | run its command by hand in the workstation with `check.sh --run` |

## Verify passes, tasks fail

Verify proves the workspace can be prepared and the connections answer. It does not run
the tests, call the model with a real ticket, or push. What shows up only in a task:

| Seen in the task | Cause | Fix |
|---|---|---|
| the result says a download or an install was refused; the task page's Network tab lists a host | the agent phase's allowlist | add the host to `network.allow` when the agent should reach it |
| the agent reports tests could not run ("Cannot connect to the Docker daemon") | tests that start containers | model what they start as `services`; say in `notes` which tests stay for CI |
| the agent ran the wrong command, or none | `notes` do not say how to run the checks, or name a command that does not work in a run | prove each command with `check.sh --run '<command>'`, then write it into `notes` |
| "pushing to <url> is not allowed for this task (push: false)" | the repository has no `push: true` | set it (with a token that can write), or leave it: the agent attaches a patch instead |
| the push is refused by the host | the token cannot write, or the branch is protected | the store key's permissions; agents push new branches, never the default one |
| the task is rejected: `unknown agent 'x' in project 'y' (it defines ...)` | the task names an agent the file no longer has | whoever sends tasks names an agent from the list, or the agent goes back into the file |
| the task is rejected because agent routing is not configured | the task names no agent and the install cannot pick one | the admin's (routing needs its own key in Settings), or the sender names an agent |
| the task was routed to the wrong agent | the roles do not say what each agent is for | rewrite the first sentence of each `role` (SKILL.md, "Roles") |
| a commit is refused by a hook (husky, pre-commit, lint-staged) | the repository's own git hooks run in a run too, after Exradius adds its trailers | a hook that needs a tool the image lacks fails the commit: install the tool in the Dockerfile, or the hook's dependencies in setup |

## Reproducing locally

```bash
bash check.sh --repo <name>=<checkout> <config folder>               # every Verify step up to setup
bash check.sh --repo <name>=<checkout> --run '<command>' <config folder>   # and a command as the agent would run it
bash check.sh --repo <name>=<checkout> --keep -v <config folder>     # keep /work, the home and /tmp to look at
```

`--platform` matters when the failure is in a build or a downloaded binary; `--live`
when the fix is an uncommitted change in the code checkout. When the local check passes
and Verify still fails, the difference is one of the things the check does not cover
(run-conditions.md, last section): say which, rather than changing the config at random.
