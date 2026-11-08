# exradius-jira design

Status: agreed 2026-11-08. Internal design note. Replaces the first Jira bridge (archived
as `exradius-jira-v0`), whose cascading Agent field the first live trial showed unusable.
Built on `exradius-bridge-kit` (its `docs/design.md`, cited below as "kit §n"); the
Exradius side is `docs/integration-api.md` and `docs/agent-routing.md` in the Exradius
repository.

## 1. What it is

A service that connects one Jira Cloud site to one Exradius installation. People **assign
an issue to the bot account**, and that is all they do: Exradius picks the agent, the
bot says which agent took it, asks the agent's questions in comments, posts the result and
assigns the issue back to its reporter.

The kit owns the flow (kit §6). This bridge is the Jira half: the tracker interface
(kit §4) over Jira Cloud's REST v3, Jira's webhooks, and Jira's text format (ADF).

Not in v1, as before: Jira Data Center, cancel, status moves (Jira Automation does them),
more than one site per bridge.

## 2. What it needs in Jira

- **One bot account**, named by the company, for example "AI Agent". An ordinary user with,
  in the spaces that use it: Browse projects, Assignable user, Assign issues, Edit issues
  (for the issue property), Add comments, Create attachments. Its email and API token are
  the bridge's Jira identity.
- **An admin webhook** (Jira settings, System, WebHooks): URL
  `<bridge public URL>/webhooks/jira`; events issue created, issue updated, comment created;
  optionally a JQL scope; a secret, so Jira signs each delivery
  (`X-Hub-Signature: sha256=<HMAC of the body>`).

Gone since v0: the admin token and the Agent field. Nothing in Jira needs Administer Jira
any more, apart from creating the webhook once.

## 3. Which project: `bridges/jira.yaml`

Each Exradius project that takes Jira work says so in its config repository
(config-repo.md §2.1):

```yaml
projects: [PLANE]
```

- `projects`: Jira space keys (`[A-Z][A-Z0-9_]*`), at least one, no repeats. It is the
  only key in v1; unknown keys are errors.
- An issue's scope (kit §3) is its space key. Moving an issue to another space moves it to
  whichever project claims that space.
- One site per bridge, so the file names no site.

The kit validates the file with this bridge's model and reports problems (a claim
conflict, an invalid key) in the bridge's status, which the Exradius panel shows.

## 4. The tracker

| Kit | Jira |
|---|---|
| `bridge`, `instance` | `"jira"`, `JIRA_SITE` |
| `ItemRef` | `id` = issue id (`10042`; it survives a move), `key` = issue key (`PLANE-101`) |
| `start()` | `GET /rest/api/3/myself` with the bot token gives the account id that identifies assignments, mentions and the bot's own comments. A `401` or `403` is a `TrackerProblem`. |
| `item()` | `GET /rest/api/3/issue/{id}` with summary, description, issue type, priority, status, labels, components, reporter, assignee, created, parent, issue links and attachments; comments from `/comment`. Description and comments are converted from ADF to Markdown (§5). `details`: type, priority, status, labels, components, created, parent (key, summary), links (relation, key, summary, status). |
| `download()` | The attachment's content URL with the bot token. |
| `post()` | `POST /comment` in ADF; `Mention` parts become ADF mention nodes. |
| `attach()` | `POST /attachments` (`X-Atlassian-Token: no-check`). |
| `hand_back()` | Only if the issue is still assigned to the bot: assign it to the reporter; if that fails (the reporter cannot be assigned), unassign it. |
| `load_state()`, `save_state()` | The issue property `exradius`: hidden from people, moves with the issue. |
| `assigned_to_bot()` | JQL `assignee = <bot account id>` through `/rest/api/3/search/jql`. |
| `replies()` | Comments after `since`, not by the bot, whose ADF (fetched through REST, never trusted from the webhook) has a mention of the bot. The mention is removed from the Markdown; attachments are those created after `since`. |

**States from v0.** Issues touched by v0 carry its `exradius` property. Its `runs` and
`task_id` mean the same in the kit's state (kit §8), so PLANE-1's next run is PLANE-1-3, and
v0-only fields (`prompted_for_agent`) are ignored.

## 5. ADF

Taken over from v0 unchanged:
- **Markdown to ADF** (markdown-it-py): headings, paragraphs, emphasis, links, inline
  code, code blocks, lists, quotes and tables; anything else becomes plain text.
- **ADF to Markdown**: paragraphs, headings, lists, code blocks, quotes, panels (as quotes),
  tables (as GFM), mentions (as the display name), links, media (as "attachment: name").

## 6. Webhooks

1. **Check** `X-Hub-Signature` over the raw body (`401` if it does not match), and drop
   repeats by `X-Atlassian-Webhook-Identifier` (Jira keeps it across its retries).
2. **Answer `200` at once**; the kit queues the work per issue. What a restart loses, the
   kit's startup reconcile catches (kit §10).
3. **Dispatch:**
   - `jira:issue_created` assigned to the bot: `handed_over`;
   - `jira:issue_updated` whose changelog changes the assignee to the bot: `handed_over`;
   - `comment_created`, by anyone but the bot: `commented`.

## 7. Configuration

Environment only, plus the kit's (kit §10: `EXRADIUS_BROKER_URL`, `EXRADIUS_API_URL`,
`EXRADIUS_INTEGRATION_TOKEN`, `MAX_ATTACHMENT_MB`):

| Variable | Meaning |
|---|---|
| `JIRA_SITE` | `https://acme.atlassian.net` |
| `JIRA_BOT_EMAIL`, `JIRA_BOT_TOKEN` | The bot account. |
| `JIRA_WEBHOOK_SECRET` | The admin webhook's secret. |
| `PORT` | Webhook listener, default `8080`. |

The integration token is created in the Exradius panel with the bridge `jira`.

## 8. Stack and layout

Python 3.14 with uv; `exradius-bridge-kit`, httpx, Starlette and uvicorn for the webhook
listener, markdown-it-py. One Docker image, running as an unprivileged user. Until the kit
has a remote, the image builds with the kit as a named build context:
`docker build --build-context kit=../exradius-bridge-kit .`

```
exradius_jira/
  main.py       settings, the kit, the listener
  settings.py   environment
  jira.py       Jira Cloud REST v3 client (from v0, without the field APIs)
  adf.py        ADF <-> Markdown (from v0)
  tracker.py    the kit's Tracker over Jira (§4)
  config.py     the bridges/jira.yaml model (§3)
  webhooks.py   signature, repeats, dispatch (§6)
tests/
```

## 9. Tests

- The tracker against a fake Jira (httpx `MockTransport`): reading an issue, posting with a
  mention, hand-back with and without a reporter, replies with and without a mention,
  state from v0.
- Webhooks: signatures, repeats, dispatch for each delivery type.
- The config model.
- The flow end to end in-process: fake Jira, the kit's `FakeBroker` and `FakeExradius`.
- A live trial on the PLANE space against the local Exradius, run together with the user.

## 10. Later

- A mention of the bot after the run ended starting a follow-up run (v0 §15 showed
  people expect it).
- Cancel by unassigning, once Exradius has a public cancel.
- Jira Data Center.
