# exradius-github

Connects GitHub issues to one [Exradius](https://github.com/musabeytekin/exradius)
installation. Add an agent label such as `agent: Developer` to an issue, and the bridge
sends it to Exradius as a task. The agent's questions come back as comments, a Quote reply
answers them, and the result is posted as a comment before the label is removed. Adding the
label again runs the agent again.

The design, and the reasons behind it, are in [docs/design.md](docs/design.md).

## What it needs

On GitHub, a GitHub App installed on the org or the repos that use agents:

- repository permissions: Issues (read and write) and Metadata (read);
- events: Issues and Issue comment;
- webhook URL `<bridge URL>/webhooks/github`, with a secret;
- a private key, downloaded from the App's settings.

In Exradius:

- an integration token (Settings, Integrations);
- the broker URL;
- the repositories in each project's `exradius.yaml`. The bridge works only in repos that a
  runnable project lists, and keeps one `agent: …` label there per agent of that project.

The bridge has no config file. Making it reachable from GitHub, and Exradius from the
bridge, is up to the installation.

## Running it

```bash
docker build -t exradius-github .
docker run --env-file bridge.env -v "$PWD/app.pem:/run/secrets/app.pem:ro" -p 8080:8080 exradius-github
```

`bridge.env` holds:

| Variable | Meaning |
|---|---|
| `GITHUB_APP_ID` | The App's id |
| `GITHUB_PRIVATE_KEY_FILE` | `/run/secrets/app.pem`, or put the PEM text in `GITHUB_PRIVATE_KEY` instead |
| `GITHUB_WEBHOOK_SECRET` | The App's webhook secret |
| `GITHUB_API_URL` | Default `https://api.github.com`; `https://<host>/api/v3` for GitHub Enterprise Server |
| `EXRADIUS_BROKER_URL` | `amqp://...` |
| `EXRADIUS_BROKER_USERNAME`, `EXRADIUS_BROKER_PASSWORD` | Optional: the broker's user, quoted into the URL (the Helm chart passes it this way) |
| `EXRADIUS_API_URL` | Exradius's public API URL |
| `EXRADIUS_INTEGRATION_TOKEN` | From the Exradius panel |
| `PORT` | Webhook listener, default `8080` |

At startup the bridge checks each of these and lists every problem it finds before it
exits.

## On Kubernetes, with the Exradius chart

The chart in `charts/exradius` runs the bridge beside the stack (values `github`). Build the
image `exradius-github` with the others (`make -C deploy/kind images`), make the bridge's
Secret in the release's namespace, then turn it on:

```bash
kubectl -n exradius create secret generic exradius-github \
  --from-file=private-key=app.pem \
  --from-literal=webhook-secret='<the App's webhook secret>' \
  --from-literal=integration-token='<a token for the bridge github>'
```

```yaml
github:
  enabled: true
  appId: "123456"
  ingress:
    enabled: true
    className: traefik
    host: exradius.example.com
```

The Ingress routes only `/webhooks/github`, so the App's webhook URL is
`https://exradius.example.com/webhooks/github`. The broker and the API come from the chart.

## Development

```bash
uv sync
uv run pytest
EXRADIUS_GITHUB_TEST_BROKER_URL=amqp://guest:guest@localhost:5673/ uv run pytest tests/test_broker.py
```

The tests run against an in-memory GitHub and broker. `tests/test_broker.py` needs a
throwaway RabbitMQ, because it declares the bridge's queue and publishes tasks.
