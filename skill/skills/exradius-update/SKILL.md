---
name: exradius-update
description: Bring an existing Exradius project config up to date with the code it describes. For a project that already has an exradius.yaml, finds what changed in the code repositories since the config was last checked against them (tool versions, package managers and registries, services, env and secret names, CI and check commands, files the config names, the team's guide) and what Exradius offers since, edits exradius.yaml, the workstation Dockerfile, notes and the admin README as a small diff, proves it with the local Docker check and publishes with the developer's go-ahead. Use when a project on Exradius got new dependencies, services, repositories or tool versions, when agents report a check they could not run or a host they could not reach, or to find out whether an exradius.yaml is still current.
argument-hint: "[config folder; default: found from the current folder]"
allowed-tools: Bash(${CLAUDE_SKILL_DIR}/scripts/changes.sh *), Bash(bash ${CLAUDE_SKILL_DIR}/scripts/changes.sh *), Bash(${CLAUDE_SKILL_DIR}/scripts/check.sh *), Bash(bash ${CLAUDE_SKILL_DIR}/scripts/check.sh *)
---

# Bring an Exradius project up to date

A project's `exradius.yaml` describes the code as it was on the day the file was written:
the tool versions in the workstation image, the services next to it, the setup commands,
the hosts the agent may reach, the commands in `notes`. The code moves on and the file
does not follow. Little of that fails loudly. Verify can pass while agents work with
Node 20 on code that now wants 22, without the search service the application gained,
from `notes` that name a script someone deleted. The agent then spends its ticket finding
out, or reports that it could not run the checks.

You close that gap: find what changed that the config depends on, change the config by
as little as that needs, prove it locally, and publish it with the developer's go-ahead.
The measure is the setup skill's: after the update an agent, on its next ticket, is where
a new engineer is after a well-run first day. And it is a diff a person reviews: an
update that rewrites the file is a failed update.

This skill is for a project that has a config. Without an `exradius.yaml`, use the
`exradius-project` skill of this plugin. With a failed Verify step or task and its log,
use that skill's "A failed Verify or task". The file must say `version: 1`; if it says
another version, stop and say this skill release does not know it.

## What to read, and when

The setup skill's references are the developer documentation for Exradius projects and
this skill uses them: `${CLAUDE_SKILL_DIR}/../exradius-project/references/` (below:
`references/`). Read them rather than guess, at the point where each is needed.

| File | Read it |
|---|---|
| `${CLAUDE_SKILL_DIR}/references/what-changes-mean.md` | with the change report in hand: per group, what to read, what it can change in the config, how to prove it |
| `references/schema.md` | before changing `exradius.yaml` |
| `references/stacks.md`, `references/run-conditions.md` | before changing a Dockerfile or the image |
| `references/network.md` | before changing `network.allow`, and when a registry, a mirror or a proxy is involved |
| `references/pitfalls.md` | its cache table when a package manager came or went; the section a symptom points at when a check step fails |
| `references/running-the-app.md` | when a service, the start of the application or what it needs changed |
| `references/admin-readme.md` | when keys, hosts, memory or what the admin must provide changed |

Two scripts, both under `${CLAUDE_SKILL_DIR}/scripts/`:

- `changes.sh` lists what changed in the code since the config was last compared with it.
  It needs git and Python 3.8 or later, reads git only, and takes seconds.
- `check.sh` is the setup skill's local check (Docker, minutes): Verify's steps, then the
  project's own commands as an agent would run them.

## Hard rules

- **Never ask for, read, print or write a secret value.** A new secret is a name:
  `${secret:NAME}` in the file, a row in the README, a line for the admin. Read only env
  files that git tracks; never an untracked `.env`, a credentials file, `~/.npmrc` or the
  shell history. `changes.sh` prints the names in env files and never the content of an
  env file or a registry configuration; when you read such a diff yourself, repeat names
  only.
- **Never call the Exradius API** or the admin panel, and never ask for an Exradius URL
  or login. What agents reported comes from the developer.
- **Leave the developer's checkouts as they are.** No stash, reset, branch switch or
  clean in a checkout you did not create. A checkout that is not at the branch head gets
  a worktree beside it (step 1).
- **A small diff.** Change what a finding requires and nothing else. Keep comments, key
  order, and the wording of roles and notes; do not reformat, rename or "improve" what
  still holds.
- **Show before you publish.** Local commits are fine; a push or a pull request happens
  only after the developer has seen the diff and said yes. Never force-push.
- **Claim only what the check showed.** A command goes into `notes` after it ran under
  the run's conditions. What was not run is listed as not run.

## 1. Find the config, the code and what changed

1. **The config**: `exradius.yaml` or `.exradius/exradius.yaml` in the current folder, in
   a sibling `../exradius-*` folder, or where the developer points. Fetch its repository
   and make sure you edit the head of the branch Exradius reads. Uncommitted changes in
   the config folder are someone's unfinished work: show them and ask before going on.
2. **The code**: a local checkout of every repository the file lists. The script finds
   the config's own repository and a sibling folder whose `origin` is the repository's
   URL; name the others with `--repo NAME=PATH`. Clone one that is missing into a
   temporary folder.
3. **The report**:

   ```bash
   bash ${CLAUDE_SKILL_DIR}/scripts/changes.sh --fetch [--repo <name>=<path> ...] <config folder>
   ```

   For each repository it compares two commits and lists only what can make the config
   stale, grouped by the part of the config it touches.

   - **since** is the commit the config was last compared with. `check/reviewed.json`
     next to the file records it (this skill and the setup skill write it since 0.6.0).
     Without the record the script estimates it from the config's own last commit, and
     says so. An estimate is wrong when that last commit was a small fix (one host added
     after a failed Verify) and not a pass over the code: look at the config's
     `git log`, and when in doubt compare from further back with `--since NAME=REV` or
     `--since NAME=YYYY-MM-DD` (the commit that created the file is always safe).
   - **until** is the head of the branch the config names, as `origin` has it: what the
     next run clones.
   - A `note:` line says what is off about a checkout: another `origin`, a default
     branch that is no longer the config's, uncommitted changes, a checkout that is not
     at the branch head.

4. **The code the check will run on** is the checkout as it is. When a note says it is
   not at the branch head: a clean checkout on that branch that is only behind is
   fast-forwarded (`git merge --ff-only origin/<branch>`); anything else gets
   `git worktree add --detach <temporary folder> origin/<branch>`, which you pass with
   `--repo` from here on and remove at the end (`git worktree remove`).

## 2. The three places drift comes from

**The code.** The report. Go through it group by group with what-changes-mean.md: read
the diff the group points at (`git -C <checkout> diff <since> <until> -- <path>`), decide
what the code needs now, and compare that with what the config gives. A finding is a
place to look, not yet an edit: most dependency bumps need nothing, and a compose service
the tests never touch may not belong in the file.

**Exradius and this skill.** Run `bash ${CLAUDE_SKILL_DIR}/scripts/check.sh --validate
<config folder>`: an error here is a key or a catalog entry that changed in Exradius
since the file was written (references/schema.md says what replaces it). Then, by the
skill release that wrote the file (the report's first lines name it), what the file may
lack:

- before 0.7.0: in the role of each agent that changes code, the sentences about proof
  from the running application (the setup skill's SKILL.md, "Roles"), and for an
  application with pages a browser shows, the `playwright` connection with what it needs
  (Chromium's libraries in the Dockerfile, the browser in setup, its cache folder, a
  screenshot at the end of `check/start-app.sh`; references/running-the-app.md, "Proof
  of a change"). The connection is a default with a cost, so propose it and let the
  developer decide; the role's sentences go in either way, in the form without a
  browser when they decline;
- before 0.6.0: `check/reviewed.json`. Nothing to fix; step 5 writes it;
- before 0.5.0: `notes` that say how to run the application, the services and system
  packages only its start needs, and `check/start-app.sh` (references/running-the-app.md);
  a test mode in `env` (`RAILS_ENV=test`), which a plain start then inherits;
- before 0.4.0: `network.allow` (the agent then reaches no package registry: propose it
  from the registries setup uses); `notes` commands that were never run under a run's
  conditions; roles that do not open with the tickets the agent takes (routing reads
  that sentence);
- before 0.2.0: `resources.memory`, `workstation.cache`, and a service `command` in place
  of a service Dockerfile that only changes the command;
- repository URLs that are not `https://`.

**The runs.** What the developer brings from Exradius: an agent's result that says a
check could not run or a host was refused (the task page's Network tab names the host),
a setup that got slow, a step killed for memory, a warning on the project. Each is a
finding with better evidence than any diff. Do not ask for these when none were given.

## 3. Let the check find what broke

Before editing anything, run the check on the config as it is and the code as it is now,
with the commands `notes` give as `--run` (the narrow form of each) and
`check/start-app.sh` as `--run-file` when the file is there:

```bash
bash ${CLAUDE_SKILL_DIR}/scripts/check.sh \
  --repo <name>=<path to checkout> [--repo ...] \
  --run '<a check command from notes>' [--run ...] \
  --run-file <config folder>/check/start-app.sh \
  <config folder>
```

It takes minutes: run it in the background and wait for it, one check at a time. Read
the report's diffs meanwhile. What fails is drift nobody has to argue about: a setup
command that no longer matches the lockfile, a test that wants the new service, a start
script that dies on a missing variable. What passes still holds: leave it alone. The
setup skill's step 4 says how to read the Memory and Network lines and what the check
does not cover; a `.exradius-cache/` folder next to the file is from before 0.5.0 (the
check still uses it; delete it to use the new place).

Skip this run only when there is nothing to find: every repository shows "no commits
since", `--validate` is clean, the record was written by this release and the developer
brought nothing. Then say the config is current, with the commits it was compared at,
and stop.

## 4. Decide the edits

From the report, the validation, what the developer brought and the failed steps, write
down each edit with its reason. what-changes-mean.md has the cases; the rules:

- **The sources rank as in setup**: CI first (it starts from a clean clone, as a run
  does), then what the team's guide tells agents to run, then lockfiles and version
  files, then Makefile and package scripts, then README.
- **Take away as well as add.** A service nothing connects to any more, an `env` entry
  the application stopped reading, a `network.allow` host and a `cache` path of a
  package manager that is gone, a `notes` line about a suite that was deleted, a
  `${secret:...}` nothing uses. Stale lines mislead an agent as much as missing ones.
- **A new secret** is referenced by name and never defaulted to a throwaway value in the
  file: the README gets its row (format, where to get it, the narrowest permission), and
  step 6 orders the publishing around it.
- **`notes` and roles** change only where a statement stopped being true or a new
  command, area or surprise appeared. A date in `notes` ("red on main in November") is
  rechecked, not carried forward.
- **`resources.memory`** follows the check's Memory line when the peak moved (the setup
  skill's rule: the highest peak, plus the running application, plus about 1 GB, at
  least `4g`). Do not lower it on one quiet measurement.

Ask the developer only what the code cannot answer, in one round, each question with the
default you would use (the AskUserQuestion tool when it is available):

- a repository that appeared (a submodule, a second checkout in CI, a path dependency):
  does it belong to the project, and may agents push to it? Default: add it read-only;
- a change in the team's guide that forbids or restricts agents, or a delivery procedure
  that now contradicts the config: quoted, with what follows. No default, and `notes`
  never tell an agent to ignore the guide;
- a host outside the registries that a new feature calls: should agents reach it?
  Default: no, and `notes` name the feature as out of reach.

With nobody to ask (a non-interactive run) take the defaults, list them as assumptions,
commit locally and do not publish.

## 5. Edit, check, record

Edit the files, commit locally (Exradius builds from the committed tree, and the check
warns about what is not in it), and run the check of step 3 again, now with the commands
you added or changed in `notes`. Rerun until it passes:

- fix the cause (the Dockerfile, a setup command, a `ready` check, an `env` value), not
  the symptom; references/pitfalls.md has the section a symptom points at;
- a new registry is proven with one command that downloads with an empty cache, a new
  `cache` entry with a second run that downloads nothing (the setup skill's step 4);
- a suite that fails is compared with CI on the same commit before the config is blamed;
- a new service is proven by something that uses it: the test that needs it as a
  `--run`, or the application's start.

When the check has passed on the branch head, record what the config now matches:

```bash
bash ${CLAUDE_SKILL_DIR}/scripts/changes.sh --record [--repo <name>=<path> ...] <config folder>
```

It writes `check/reviewed.json` (each repository's commit, and this skill's release).
Exradius does not read the file; the next update compares from it. Commit it with the
edits. Update `README.md` when keys, hosts, memory, caches or what the admin must provide
changed, and `check/start-app.sh` when the start did.

## 6. Show, then publish

Show the developer, briefly:

- what changed in the code that mattered, one line per finding, and what you looked at
  and left alone;
- the diff of the config repository, with the reason for each edit;
- the check's result: the steps, each `--run` command and its outcome, how the
  application answered, the memory peak, the hosts added or dropped;
- the assumptions, and anything that could not be verified.

Then the order of publishing, which a new key changes. Exradius reads the branch head for
every task: from the moment a config that references a key the store does not hold is on
that branch, every task of the project is rejected for the missing key.

- **No new key**: with the go-ahead, push to the config repository's branch, or open a
  pull request when the branch is protected or the config lives in `.exradius/`.
- **A new key**: push a branch and open a pull request (or hold the push), give the
  developer the line for the admin, and merge once the admin has added the key.
- A key that is no longer referenced can leave the store after the merge; say which.

Tell the developer what the push sets off: Verify reruns by itself on the new commit, a
changed Dockerfile is built before the next run starts, a changed `cache` path starts
cold once. End with the message for the admin when there is one: the keys to add (name,
format, where to get it, the permission it needs) and to remove, and what only the
install can provide (a private address to allow, a pull secret). When nothing concerns
the admin, say that instead.

## When nothing needs to change

Say so, with what it rests on: the commits compared for each repository, that
`--validate` is clean, and whether the check ran and passed on the branch head. When it
did, record it (step 5) so the next update starts from here; the record is a commit like
any other and goes out with the developer's go-ahead (Exradius reruns Verify on it).
