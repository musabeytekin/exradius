# exradius-jira

Connects one Jira Cloud site to one [Exradius](https://github.com/musabeytekin/exradius)
installation, on [the bridge kit](../kit). Assign a Jira issue to the
bot account: Exradius picks the agent, the bot says which one took it, asks the agent's
questions in comments (mention the bot to answer), posts the result and assigns the issue
back to its reporter.

Which Exradius project takes a Jira space is declared in that project's config repository,
in `bridges/jira.yaml`:

```yaml
projects: [PLANE]
```

The design is in [docs/design.md](docs/design.md). It replaces the first Jira bridge, kept
as `exradius-jira-v0`.

## What it needs

- In Jira: a bot account (Browse projects, Assignable user, Assign issues, Edit issues,
  Add comments, Create attachments in the spaces that use it) and an admin webhook to
  `<bridge URL>/webhooks/jira` for issue created, issue updated and comment created, with
  a secret.
- In Exradius: an integration token for the bridge `jira` (Settings, Integrations).

## Environment

| Variable | |
|---|---|
| `JIRA_SITE` | `https://acme.atlassian.net` |
| `JIRA_BOT_EMAIL`, `JIRA_BOT_TOKEN` | The bot account. |
| `JIRA_WEBHOOK_SECRET` | The admin webhook's secret. |
| `EXRADIUS_BROKER_URL` | The Exradius broker. |
| `EXRADIUS_API_URL` | Exradius's API, for the directory and the status report. |
| `EXRADIUS_INTEGRATION_TOKEN` | The `jira` token. |
| `MAX_ATTACHMENT_MB` | Default `10`. |
| `PORT` | Default `8080`. |

## Build and test

```bash
uv sync && uv run pytest
docker build --build-context kit=../kit -t exradius-jira .
```
