# exradius-github design

Status: agreed 2026-11-16. Internal design note. Replaces the first GitHub bridge (a GitHub
App started by agent labels, 2026-11-08), which predated the bridge kit and agent routing.
Built on `exradius-bridge-kit` (`bridges/kit/docs/design.md`, cited below as "kit §n") the
way the Jira bridge is (`bridges/jira/docs/design.md`); the Exradius side is
`docs/integration-api.md` and `docs/agent-routing.md`.

## 1. What it is

A service that connects GitHub issues to one Exradius installation. People **assign an
issue to the bot account**, as they would to a colleague, and that is all they do: Exradius
picks the agent, the bot says which agent took it, asks the agent's questions in comments,
posts the result and assigns the issue back to its author.

The kit owns the flow (kit §6). This bridge is the GitHub half: the tracker interface
(kit §4) over GitHub's REST API, GitHub's webhooks, and GitHub-flavoured Markdown.

Not in v1, as in Jira: cancel, a mention after the run ended starting a follow-up run,
pull requests (assigning a pull request to the bot does nothing), GitHub Apps.

## 2. What it needs on GitHub

- **One bot account**, an ordinary GitHub user the company names (GitHub's terms allow
  one free machine account per person). It is a collaborator on the repositories that use
  it, or a member of the organization with write access to them: GitHub only lets people
  assign issues to accounts with access, and the bot comments and reassigns.
- **Its token**: a classic personal access token with `repo` (`public_repo` when every
  repository is public). Fine-grained tokens cannot reach a personal account's repository
  where the bot is only a collaborator. The token never leaves the bridge.
- **A webhook** on each repository, or one on the organization: URL
  `<bridge public URL>/webhooks/github`, content type `application/json`, a secret, events
  *Issues* and *Issue comments*. GitHub signs each delivery
  (`X-Hub-Signature-256: sha256=<HMAC of the body>`).

Why not a GitHub App: an App can be neither assigned nor @mentioned, and people hand work
to a colleague by assigning it. The bot account is that colleague.

## 3. Which project: `bridges/github.yaml`

Each Exradius project that takes GitHub work says so in its config repository
(config-repo.md §2.1):

```yaml
repositories: [acme/plane]
```

- `repositories`: `owner/name`, at least one, no repeats, compared without case. It is the
  only key in v1; unknown keys are errors.
- An issue's scope (kit §3) is its repository. One GitHub host per bridge
  (`GITHUB_API_URL`), so the file names no host.

## 4. The tracker

| Kit | GitHub |
|---|---|
| `bridge`, `instance` | `"github"`, `https://github.com` (or the Enterprise Server's root) |
| `ItemRef` | `id` = `owner/name#12` (lower case), `key` = `name-12`: task ids `plane-12`, `plane-12-2` |
| `start()` | `GET /user` with the token gives the bot's login, which identifies assignments, mentions and the bot's own comments. A `401` or `403` is a `TrackerProblem`. |
| `item()` | `GET /repos/{repo}/issues/{n}` and its comments. `details`: repository, number, state, labels, assignees, milestone, created. A `404` or `410` is `ItemGone`. |
| `post()` | `POST .../comments`, the kit's message as Markdown; a mention is `@login`. |
| `attach()` | GitHub issues have no attachments API. A text file is held and put into the next comment (the result's) as a `<details>` block, up to 50,000 characters in all; anything else fails and the kit names it as not attached. |
| `hand_back()` | Only if the bot is still an assignee: add the issue's author (GitHub leaves out, silently, an author who cannot be assigned), then remove the bot. |
| `load_state()`, `save_state()` | §5. |
| `assigned_to_bot()` | `GET /issues?filter=assigned&state=open`, pull requests left out: the startup reconcile. |
| `replies()` | Comments after `since`, not by the bot, that mention `@<bot>` (read through the API, never trusted from the webhook). The mention and the lines a Quote reply copies (`> ...`) are taken out. |

Files people put in an issue are links in its Markdown (GitHub uploads them for the
editor); the agent sees the links. `Item.attachments` is always empty.

## 5. State

GitHub issues have no hidden properties, so the kit's state (kit §8) rides in the bot's
own comments as an HTML comment that never renders:
`<!-- exradius:<base64 JSON> -->` at the end of the body.

- **Load**: the bot's latest comment that carries one.
- **Save**: edit the bot's latest comment on the issue, replacing its marker. When the bot
  has not commented there yet (the kit saves `starting` before publishing a task), it posts
  "Exradius is picking an agent for this." with the marker, which is also what people see
  until the agent's start comment.
- People cannot write the state: only the bot's comments are read, and people cannot edit
  them (repository admins aside).

## 6. Webhooks

- The signature is checked first; a bad one is `401`. `X-GitHub-Delivery` is remembered
  (the last 4,096), so a redelivery is not acted on twice. `ping` answers `pong`.
- `issues` with `assigned` and `assignee` the bot: `handed_over`. Opening an issue with the
  bot as an assignee sends `assigned` too, so `opened` is not used.
- `issue_comment` with `created`, not by the bot: `commented`.
- Deliveries for pull requests are ignored.

## 7. Settings

| Variable | Meaning |
|---|---|
| `GITHUB_TOKEN` | The bot account's token (§2) |
| `GITHUB_WEBHOOK_SECRET` | The webhooks' secret |
| `GITHUB_API_URL` | Default `https://api.github.com`; `https://<host>/api/v3` for Enterprise Server |
| `EXRADIUS_BROKER_URL`, `EXRADIUS_API_URL`, `EXRADIUS_INTEGRATION_TOKEN`, `MAX_ATTACHMENT_MB` | The kit's (kit §10) |
| `EXRADIUS_BROKER_USERNAME`, `EXRADIUS_BROKER_PASSWORD` | Optional: the broker's user, quoted into the URL (how the Helm chart passes it) |
| `PORT` | Default `8080` |

Every problem is listed at once before the bridge exits.

## 8. Package and tests

Python 3.14, `exradius_github/`: `github.py` (the REST client), `tracker.py`,
`webhooks.py`, `config.py`, `settings.py`, `main.py`. The image builds with the kit as a
named build context, like the Jira bridge's. The Helm chart runs it as the optional
`github` component (values `github`), with an Ingress for `/webhooks/github` only.

Tests run against an in-memory GitHub (`tests/fakes.py`: tokens, collaborators, issues,
comments, pagination) and the kit's fake broker and Exradius: the tracker's calls, the
state markers, replies and mentions, webhooks, and the flow from assignment to result.
