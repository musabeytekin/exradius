# Evaluation dataset

Real upstream bugs for the system-level evaluation sketched in `docs/ideas.md` ("Evaluating
Exradius on real tasks"): a ticket goes in as its reporter wrote it, Exradius runs it, and the
result is graded against the fix upstream merged, which the agent never sees.

This directory holds the dataset only. The runner and the grader are not written yet.

## Layout

- `projects.yaml`: the model, and for each project the upstream repositories and the commit a
  run starts from.
- `tasks/<id>.yaml`: one bug: the issue, the upstream fix, how it is graded.
- `fetch_tickets.py`: writes each issue's text to `tickets/<id>.md` (ignored by git) and
  checks it against the hash in the task file.

## How the tasks were chosen

Every agent in the evaluation runs `claude-opus-5-5`, whose training data ends in June 2026.
A task is an upstream pull request that:

- was merged into the default branch after 2026-06-30, so the model cannot know the fix;
- closes an issue, whose title and body are the ticket (comments are left out);
- changes tests next to the code, except where the task says `grading: review`;
- applies to the project's base commit without conflicts in source files (`saleor-19890` is
  the one exception; its notes say why).

## Projects

Three are set up on Exradius (`configured: true`) and hold the fourteen `selected` tasks: four
each, and one more in Saleor and in Outline about the running application (below):

| Project | Stack | Repositories |
|---|---|---|
| plane | Django, React | makeplane/plane |
| saleor | Django GraphQL, React | saleor/saleor, saleor/saleor-dashboard |
| outline | Node, React | outline/outline |

Five more have no config repository yet. Each holds three `candidate` tasks; Keycloak has one
more and Home Assistant two more about the running application:

| Project | Stack | Repositories | What it adds |
|---|---|---|---|
| gitea | Go | go-gitea/gitea | Go, integration tests against a database |
| ruff | Rust | astral-sh/ruff | Rust, no services, snapshot tests |
| keycloak | Java, Maven | keycloak/keycloak | a long build, tests that start a server |
| immich | NestJS, Svelte | immich-app/immich | reports that describe only the symptom |
| home-assistant | Python, TypeScript | home-assistant/core, home-assistant/frontend | the one fix that spans two repositories, and the hardest task |

They were picked from 58 projects screened for pull requests that close an issue; the pool of
small fixes with tests is 147 in Gitea, 52 in Ruff, 120 in Keycloak, 111 in Immich and 245 in
Home Assistant core.

## Fields of a task

- `status`: `selected` (in the first set), `candidate` (chosen, in a project that is not set
  up yet) or `reserve` (to replace a task that fails validation).
- `issue.upstream`, `issue.number`: where the ticket comes from. `issue.sha256` is the hash of
  the ticket text (`# <title>`, an empty line, the body).
- `area`: `api`, `backend`, `frontend`, `editor`, `cli` or `cross-repo`.
- `hint`: how much the report gives away: `symptom` (what the reporter saw), `location` (it
  names the file or function, or shows a stack trace), `fix` (it says what to change).
- `grading`: `tests` (the fix's tests, run on the agent's branch), `review` (the agent's diff
  read against the upstream fix: correct, partial or wrong), or `tests+review`.
- `fixes`: one entry per repository the fix changed; only `home-assistant-frontend-54304` has
  two. In each:
  - `commit`: the merge commit; its diff against its parent is the gold patch, without
    `CHANGELOG.md`, `AGENTS.md`, `.changeset/`, `.github/` and `docs/`.
  - `source_files`, `test_files`: what the gold patch changes.
  - `applies_on_base`: whether the gold patch merges onto the base commit (`git merge-tree`);
    `conflicts` lists the files when it does not. The notes say why a conflict is harmless.
- `runtime` (six tasks): what has to be started to see the failure the way the report
  describes it. Absent where a test is enough.
- `validated`: whether the fix's tests were seen to fail on the base and pass with the gold
  patch, in the project's own workstation. False for every task so far.

## Tasks about the running application

Most reports here can be answered by reading the code and writing a test, and the tests grade
only the change. Six tasks are in the set for the other question, whether the agent brings the
application up and debugs it there. Their failure belongs to the started application:

| Task | What fails |
|---|---|
| `saleor-19890` | the server does not start without the dev dependencies |
| `outline-14354` | an export fails at its end, on a column limit in the database; the fix ships a migration |
| `keycloak-54455` | a second `kc.sh build` keeps the first one's extensions, and the API answers 500 |
| `home-assistant-core-184125` | an integration never loads when its service is down at startup |
| `home-assistant-core-182861` | every unauthenticated request answers 500 instead of 401 |
| `home-assistant-core-186743` | startup hangs for minutes |

Only `saleor-19890` is graded by starting anything: upstream's checks start uvicorn and a
Celery worker. For the other five the tests stage the failure, so a run can still pass without
starting the application. What the agent did is read from the transcript, for every task in
the set and these six first: whether it started the application or its tests, and whether it
saw the failure before the fix and its absence after.

## Bases

A project's config names one branch per repository and a task cannot name a commit, so each
repository has one base for all its tasks: the parent of the earliest fix in the pool. A bug
whose fix is later than the base is assumed to exist there because its gold patch applies;
validation proves it.

This costs tasks. A fix merged weeks after the base often conflicts with it, in its tests or
in its source: seven tasks here still have a conflict, four of them in the test files the
grader needs and one, `saleor-19890`, in a source file, and a dozen candidates (in Immich, Keycloak, Gitea, Outline and Saleor) were dropped
because their fix conflicts with the base or precedes it.
If a task could name the commit it starts from, every task would start at its fix's parent and
none of this would apply.

## What the set does not have

- More than one task that spans repositories. No upstream fix in the window spans Saleor and
  its dashboard; the one found is in Home Assistant.
- Many hard tasks. Most fixes change one to three source files; `saleor-19983` is one line and
  `home-assistant-core-186743` is the only one above 400 lines.
- A task that cannot be solved without starting the application. In every report found, the
  cause can be read from the code or the report states it; the six tasks above are the closest.
  Plane has none: its candidates were in the Docker image's start script and in the browser.
- A failure the agent can see in a browser. The workstation has none, so `plane-9612` and the
  frontend half of `home-assistant-frontend-54304` are reviewed.
- Tests that can grade any correct fix. Ruff's snapshots fix the exact output, and some
  upstream tests call helpers the fix introduced; those tasks are `review` or `tests+review`.

## Before a first run

1. Validate each task in its project's workstation: the tests in `test_files` fail on the base
   and pass with the gold patch. Record which tests are fail-to-pass. `saleor-19890` needs its
   gold patch ported to the base by hand, and an environment without the dev dependency group
   for its checks.
2. Give each repository an evaluation copy that holds the history up to the base and nothing
   later. The forks the configured projects use today contain the fixes on their default
   branches.
3. Give each project a config branch that points at those copies and sets
   `model: claude-opus-5-5` on its agents. The five new projects need a config repository
   first.
4. Decide how a run that reads the upstream repository is detected. `github.com` is reachable
   in the agent phase, and the upstream fix is public.
