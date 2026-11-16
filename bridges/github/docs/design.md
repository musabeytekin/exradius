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

After a run, a comment that mentions the bot starts a follow-up run (kit §6.4), as asking
the colleague again would; the issue stays with whoever has it.

Not in v1, as in Jira: cancel, pull requests (assigning a pull request to the bot does
nothing), GitHub Apps.

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
| `attach()` | §4.1. |
| `hand_back()` | Only if the bot is still an assignee: add the issue's author (GitHub leaves out, silently, an author who cannot be assigned), then remove the bot. |
| `load_state()`, `save_state()` | §5. |
| `assigned_to_bot()` | `GET /issues?filter=assigned&state=open`, pull requests left out: the startup reconcile. |
| `replies()` | Comments after `since`, not by the bot, that mention `@<bot>` (read through the API, never trusted from the webhook). The mention and the lines a Quote reply copies (`> ...`) are taken out. |

### 4.1 Files a run produced

GitHub's API has no way to attach a file to an issue or a comment: the editor's uploads
(`github.com/user-attachments/...`) take a browser session, not a token. So `attach()`:

- holds a **text file** and puts it into the next comment (the result's) as a `<details>`
  block, while the inlined text stays within 50,000 characters;
- commits **anything else** (and text past that) to the **artifacts branch**,
  `exradius-artifacts` by default (`GITHUB_ARTIFACTS_BRANCH`), at `<item key>/<name>`, with
  the git data API as the bot: blob, tree on the branch's head, commit, fast-forward the
  ref, and again on top when someone moved it meanwhile (three tries). The first time it
  makes the branch as an orphan, with no history of the code, and a README saying what it is
  and that it can be deleted. Files up to 50 MB.
- shows the file in that comment, linked to `https://github.com/<repo>/blob/<commit>/<path>?raw=true`:
  an image (`.png`, `.jpg`, `.jpeg`, `.gif`, `.webp`) as an image, the rest as a link. The
  link is pinned to its commit, so a later run's file of the same name does not change an
  earlier comment.

The files keep the repository's own access: in a private repository only people who can
read it see them. The bot needs write access, which assigning issues needs anyway; a branch
rule that blocks it makes the file "could not attach", as before. This is a stopgap (§9).

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
| `GITHUB_ARTIFACTS_BRANCH` | Default `exradius-artifacts` (§4.1) |
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

## 9. Later

- **Files without a branch.** The artifacts branch (§4.1) works but writes to people's
  repositories, and the files outlive the issue. Better would be a place Exradius itself
  serves to the people who can see the issue: a short-lived, per-viewer link through the
  panel's sign-in, or an object store link scoped to the issue. It needs Exradius to serve
  artifacts to people outside the admin, which it does not do yet.
- A mention on an issue that never ran starting its first run (today: assign it).
