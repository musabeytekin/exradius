# exradius-github

Connects GitHub issues to one [Exradius](https://github.com/musabeytekin/exradius)
installation through a bot account. Assign an issue to the bot as you would to a colleague:
Exradius picks the agent, the bot says which agent took it, the agent's questions come back
as the bot's comments, a comment that mentions the bot answers them, and the result is
posted before the issue is assigned back to its author. Mentioning the bot afterwards, or
assigning the issue again, runs it again.

GitHub issues take no attachments through the API, so text files a run produced go into the
result comment, and anything else (screenshots, binaries) is committed to a branch of the
repository, `exradius-artifacts`, and shown from there (design §4.1).

When someone requests changes in a review of a pull request the agent opened, the agent
picks it up on the same branch, and that run talks on the pull request, not the issue. The
bridge keeps which task opened which pull request in its own SQLite database, on a volume,
each row for 30 days after its last use (design §10, §11).

The design is in [docs/design.md](docs/design.md). It is built on the bridge kit
([../kit](../kit)), like the Jira bridge.

## What it needs

On GitHub:

- a bot account, a collaborator on the repositories that use it (or an organization
  member with write access to them);
- a classic personal access token of the bot account with `repo` (`public_repo` if every
  repository is public);
- a webhook on each repository (or one on the organization): URL
  `<bridge URL>/webhooks/github`, content type `application/json`, a secret, events
  *Issues*, *Issue comments*, *Pull requests* and *Pull request reviews*.

In Exradius:

- an integration token for the bridge `github` (Settings, Integrations);
- in each project's config repository, `bridges/github.yaml` naming its repositories:

  ```yaml
  repositories: [acme/plane]
  ```

## On Kubernetes, with the Exradius chart

The chart in `charts/exradius` runs the bridge beside the stack (values `github`). Build
the image `exradius-github` with the others (`make -C deploy/kind images`), make the
bridge's Secret in the release's namespace, then turn it on:

```bash
kubectl -n exradius create secret generic exradius-github \
  --from-literal=bot-token='<the bot account token>' \
  --from-literal=webhook-secret='<the webhook secret>' \
  --from-literal=integration-token='<a token for the bridge github>'
```

```yaml
github:
  enabled: true
  ingress:
    enabled: true
    className: traefik
    host: exradius.example.com
```

The Ingress routes only `/webhooks/github`, so the webhook URL is
`https://exradius.example.com/webhooks/github`. The broker and the API come from the chart.

## Running it alone

```bash
docker build --build-context kit=../kit -t exradius-github .
docker run --env-file bridge.env -p 8080:8080 exradius-github
```

`bridge.env` holds `GITHUB_TOKEN`, `GITHUB_WEBHOOK_SECRET`, `EXRADIUS_BROKER_URL`,
`EXRADIUS_API_URL` and `EXRADIUS_INTEGRATION_TOKEN`, and optionally `GITHUB_API_URL`
(`https://<host>/api/v3` for GitHub Enterprise Server), `GITHUB_ARTIFACTS_BRANCH`,
`GITHUB_DB_PATH` (default `/data/exradius-github.db`; mount a volume at `/data`),
`GITHUB_DB_TTL_DAYS`, `MAX_ATTACHMENT_MB` and `PORT` (design §7). At startup the bridge lists every problem it finds before it exits.

## Development

```bash
uv sync
uv run pytest
```
