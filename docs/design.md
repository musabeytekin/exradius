# exradius-github design

Status: agreed 2026-11-08. Internal design note. It follows the Jira bridge
(`exradius-jira/docs/design.md`) wherever GitHub allows, and says where it differs. The
Exradius side it relies on is `docs/integration-api.md` in the Exradius repository.

## 1. What it is

A service that connects GitHub issues to one Exradius installation:
- someone adds an agent label to an issue;
- the bridge sends the issue to Exradius as a task;
- it posts the agent's questions as comments and passes Quote replies back as answers;
- it posts the result, then removes the label.

It speaks only Exradius's public contract (the broker and the integration API) and imports
nothing from Exradius.

Not in v1:
- **Pull requests.** Only issues; labels on pull requests are ignored.
- **Cancel.** Exradius has no public cancel.
- **Status moves.** GitHub Projects automation can react to labels and comments.
- **Downloading images and files from issue bodies.** GitHub has no attachments API: files
  in an issue are links in its Markdown, and the agent gets them as links (§4).
- **More than one GitHub host per bridge, or more than one bridge instance.**

## 2. What it needs on GitHub

- **A GitHub App** registered by the company and installed on the org (or the repos) that
  use agents.
  - Repository permissions: Issues read and write (covers comments and labels), Metadata read.
  - Events: Issues, Issue comment.
  - Webhook URL `<bridge public URL>/webhooks/github`, with a secret.

  The app's private key, app id and webhook secret go to the bridge. The app comments as
  `<slug>[bot]` and takes no seat.
- **Why an App and not a user account.** An App cannot be assigned an issue or be
  @mentioned. So a label starts a task (§3.1), and a Quote reply answers a question (§3.2).
- **Which repos.** Exradius's directory lists each project's repositories. The bridge works
  only in repos that are on this GitHub host, belong to a runnable project and have the App
  installed. Issues anywhere else are ignored.

Making the bridge reachable from GitHub, and Exradius from the bridge, is the installer's
job.

## 3. Flow

### 3.1 Agent labels and starting a task

**Labels.** In each repo in scope, the bridge keeps one label per agent of the repo's
project:
- **Name.** `agent: Developer`. If the repo belongs to several projects, the name includes
  the project: `agent: Plane · Developer`. Names that clash get the slug, as in Jira, and
  are cut to GitHub's 50 characters.
- **Description.** `Exradius plane/developer`. This is how the bridge recognises its own
  labels and maps a label back to its slugs. Labels without such a description are never
  touched.
- **Removed and non-runnable agents.** Labels for agents that were removed, or for projects
  that are not runnable, are deleted. GitHub labels cannot be disabled, and a label that
  could only be rejected should not be offered.

**Adding an agent label starts a task** (the `issues` webhook, action `labeled`):
- **A run is already active on the issue:** the bot comments "**Plane · Developer** is still
  working on this." and removes the label that was just added.
- **The label maps to an agent that is no longer in the directory:** the bot says so and
  removes the label.
- **Otherwise:**
  1. The bot posts "**Plane · Developer** is working on this." That comment holds the run's
     state (§5).
  2. The bridge publishes the task (§4).
  3. If publishing fails, the comment is edited to say the task could not start, and the
     label is removed.

The person who added the label is recorded as the requester. Questions and results mention
them.

### 3.2 Questions and answers

On `task.info_request`, the bot comments:

> **Plane · Developer** asks @requester:
>
> *question*
>
> *context, if any*
>
> Use Quote reply on this comment to answer.

**A comment that quotes the question is the answer.** GitHub's Quote reply prefixes the
quoted text with `>`. The bridge compares the comment's quoted lines with the lines of the
question comment, ignoring Markdown emphasis, spacing and case. A quoted line of at least
eight characters that appears in the question is enough, so quoting only part of the
question works too. The answer is the comment with its quoted lines removed.

Only the first such comment after the question counts. Comments without a quote, and
comments by the App, are ignored. Edits are ignored in v1.

### 3.3 Results

On `task.result`, the bot posts one comment:

| State | Comment |
|---|---|
| `completed` | "**Plane · Developer** finished. @requester", then the summary and the warnings. |
| `failed` | "**Plane · Developer** could not finish: *error*. @requester", then the summary. |
| `cancelled` | "**Plane · Developer** was stopped. @requester" |

A `task.status` with state `rejected` is reported the same way ("could not start"), except
for duplicate ids (§4).

**Artifacts.** GitHub has no API to attach files to an issue. So text artifacts (Markdown,
plain text, patches, diffs, JSON, YAML, logs) of up to 60 000 characters each are inlined in
collapsed `<details>` blocks, while the comment stays under GitHub's 65 536-character limit.
Any other artifact is listed by name as not attached. The transcript link is not posted.

After a result or a rejection the bridge **removes the agent label**, so adding it again
reruns. Assignees are left alone.

## 4. The task message

| Field | From GitHub |
|---|---|
| `task_id` | `<repo>-<number>` for the first run (`plane-123`), then `plane-123-2`, `-3`. It is a valid branch-name prefix, which `owner/repo#123` is not. |
| `project`, `agent` | From the label's description, checked against the directory and the issue's repository. |
| `instructions` | The title, a blank line, then the body. Both are Markdown already. |
| `context` | `issue`: repository, number, url, author, state, labels other than agent labels, created. `comments`: every comment's author, time and body, without the bridge's state markers. |
| `attachments` | None. Images and files stay links in the body. |
| `correlation` | `{"github": {"host": "github.com", "repository": "acme/plane", "issue": 123, "installation": 456}}` |

**Duplicate ids** are handled as in Jira. A rejection whose reason starts with
`duplicate task_id` bumps the run counter and republishes, up to five times. The start
comment is edited, not reposted.

## 5. State

GitHub issues have no hidden properties. **The run's state rides in the bot's own comments**
as a hidden HTML comment, base64-encoded JSON:
`<!-- exradius-github:eyJydW5zIjoxLC4uLn0= -->`.

```json
{
  "runs": 1,
  "task_id": "plane-123",
  "project": "plane",
  "agent": "developer",
  "label": "Plane · Developer",
  "agent_label": "agent: Developer",
  "requester": "octocat",
  "state": "waiting",
  "request_id": "a7c1...",
  "retries": 0
}
```

- **Current state.** The issue's state is the marker in the bot's latest comment that has
  one. Each step that posts a comment (start, question, result) carries the new state.
- **Steps that post nothing** (an answer passed on, a duplicate-id retry) edit the marker in
  that latest comment.
- **Not visible.** The marker never shows on GitHub, and Quote reply never copies it.
- **No database.** The bridge keeps no database. A per-issue in-process lock serialises
  webhook and event handlers for the same issue.

## 6. Events, webhooks, startup

**Events.**
- The bridge consumes the durable queue `exradius-github.events`, bound to `task.*` and
  `directory.changed`.
- It ignores task events whose `correlation.github.host` is not its own host.
- Events are handled one at a time, and a failing one is retried once, then dropped.
- `directory_changed`: fetch the directory and sync the labels.

**Webhooks.**
- Each delivery's `X-Hub-Signature-256` (`sha256=` HMAC of the raw body) is verified, and
  repeats are dropped by `X-GitHub-Delivery`.
- The bridge answers `200` at once and handles the delivery in the background.
- What it acts on:
  - `issues`, action `labeled`, with one of its labels: start a task (§3.1);
  - `issue_comment`, action `created`, not by the App: check for an answer (§3.2);
  - `installation` and `installation_repositories`: sync the labels, because the repos in
    scope changed.

**Startup.**
1. Read the environment (§7).
2. Sign in as the App (a JWT from the private key). `GET /app` gives the slug, and from it
   the bot's login.
3. Fetch the directory, connect to the broker, and declare the queue.
4. Sync the labels in every repo in scope.
5. Reconcile, for every agent label in every repo in scope: list the open issues that carry
   it. An issue with no active run had its label added while the bridge was down, and
   starts. An issue that is waiting gets its comments checked for an answer.
6. Consume events and serve webhooks.

The bridge stops at any failure and lists every problem.

**Installation tokens** are fetched per installation and renewed five minutes before they
expire, after an hour.

## 7. Configuration

The bridge takes only environment variables; there is no config file.

| Variable | Meaning |
|---|---|
| `GITHUB_APP_ID` | The App's id. |
| `GITHUB_PRIVATE_KEY_FILE` or `GITHUB_PRIVATE_KEY` | The App's private key, as a file path or as the PEM text. |
| `GITHUB_WEBHOOK_SECRET` | The App's webhook secret. |
| `GITHUB_API_URL` | Default `https://api.github.com`; `https://<host>/api/v3` for GitHub Enterprise Server. The host that repository URLs are matched against is derived from it. |
| `EXRADIUS_BROKER_URL`, `EXRADIUS_API_URL`, `EXRADIUS_INTEGRATION_TOKEN` | As for the Jira bridge. |
| `PORT` | Webhook listener, default `8080`. |

## 8. Layout and tests

Python 3.14 with uv, aio-pika, httpx, pydantic, PyJWT with cryptography, Starlette and
uvicorn. It ships as one Docker image.

```
exradius_github/
  main.py       startup, wiring
  settings.py   environment
  github.py     App auth (JWT, installation tokens) and the REST calls used
  exradius.py   broker and directory client (a copy of exradius-jira's, plus repositories)
  repos.py      repository URLs from the directory, mapped to GitHub repos
  labels.py     agent labels: names, sync, mapping back
  state.py      the hidden state marker
  replies.py    Quote reply detection
  flow.py       start, questions, answers, results
  webhooks.py   signature, repeats, dispatch
```

Tests run against a fake GitHub (httpx `MockTransport`, with real JWT checks against a test
key) and a fake broker. A live trial on a real repo, against a local Exradius, is run
together with the user.
